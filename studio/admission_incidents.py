"""Task-admission intake incident 的持久生命週期。

Worker 只提交本輪任務邊界的最終觀察值；本模組封裝跨程序鎖、原子 ledger、
通知 outbox 與安全的 operator 投影，不自行推測 control state 是否已恢復。
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import logging
import math
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import config, secure_write

log = logging.getLogger("ti.admission_incidents")

SCHEMA_VERSION = 1
_MAX_LEDGER_BYTES = 64 * 1024
_MAX_SAFE_INT = (1 << 63) - 1
_SAFE_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")
_MODES = frozenset({"off", "shadow", "enforce"})

# 唯一寫入 choke point；保留 module-level alias 供 fault-injection 測試。
secure_write_root = secure_write.secure_write_root


@dataclass(frozen=True, slots=True)
class Faulted:
    """Worker 已判定本輪不得 intake。"""

    error_code: str
    effective_mode: str
    effective_generation: int


@dataclass(frozen=True, slots=True)
class Waiting:
    """正常任務邊界等待；不開啟也不關閉 incident。"""

    reason: str = "mode_switch"


@dataclass(frozen=True, slots=True)
class IntakeRestored:
    """Worker 已更新 runtime pin，且本輪即將允許 intake。"""

    effective_mode: str
    effective_generation: int


AdmissionObservation = Faulted | Waiting | IntakeRestored


@dataclass(frozen=True, slots=True)
class IncidentEvent:
    """交給外部通知 adapter 的去敏事件。"""

    event_id: str
    kind: str
    title: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class IncidentReceipt:
    """一次觀察的處理結果；永不拿來改寫 worker intake 判定。"""

    incident_id: str | None
    phase: Literal["none", "open", "recovered"]
    durability: Literal["durable", "memory_only"]
    notification: Literal["queued", "deduped", "deferred"]
    diagnostic: str = ""


NotificationSink = Callable[[IncidentEvent], bool | None]


@dataclass(slots=True)
class _MemoryLedger:
    ledger: dict[str, Any]
    diagnostic: str
    base_ledger: dict[str, Any] | None
    latest_observation: AdmissionObservation | None


_memory_guard = threading.RLock()
_memory_ledgers: dict[str, _MemoryLedger] = {}
_process_locks: dict[str, threading.RLock] = {}
_accepted_event_ids: dict[str, set[str]] = {}
_delivery_context = threading.local()
_KEEP = object()


class _LedgerError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _dir(state_dir: Path | None) -> Path:
    return Path(state_dir) if state_dir is not None else config.AUTOPILOT_STATE_DIR


def _path(state_dir: Path | None) -> Path:
    return _dir(state_dir) / "admission_incident.json"


def _lock_path(state_dir: Path | None) -> Path:
    return _dir(state_dir) / "admission_incident.lock"


def _delivery_lock_path(state_dir: Path | None) -> Path:
    return _dir(state_dir) / "admission_incident_delivery.lock"


def _memory_key(state_dir: Path | None) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(_dir(state_dir))))


@contextlib.contextmanager
def _process_locked(state_dir: Path | None):
    """序列化同一實體 state dir 的 durable 與 memory read-modify-write。"""
    key = _memory_key(state_dir)
    with _memory_guard:
        lock = _process_locks.setdefault(key, threading.RLock())
    with lock:
        yield


def _memory_get(state_dir: Path | None) -> _MemoryLedger | None:
    with _memory_guard:
        entry = _memory_ledgers.get(_memory_key(state_dir))
        return copy.deepcopy(entry) if entry is not None else None


def _memory_set(
    state_dir: Path | None,
    ledger: dict[str, Any],
    diagnostic: str,
    *,
    base_ledger: dict[str, Any] | None | object = _KEEP,
    latest_observation: AdmissionObservation | None | object = _KEEP,
) -> None:
    with _memory_guard:
        key = _memory_key(state_dir)
        previous = _memory_ledgers.get(key)
        if base_ledger is _KEEP:
            base_ledger = previous.base_ledger if previous is not None else None
        if latest_observation is _KEEP:
            latest_observation = previous.latest_observation if previous is not None else None
        _memory_ledgers[key] = _MemoryLedger(
            ledger=copy.deepcopy(ledger),
            diagnostic=diagnostic,
            base_ledger=copy.deepcopy(base_ledger),
            latest_observation=copy.deepcopy(latest_observation),
        )


def _memory_clear(state_dir: Path | None) -> None:
    with _memory_guard:
        _memory_ledgers.pop(_memory_key(state_dir), None)


def _accepted_get(state_dir: Path | None) -> set[str]:
    with _memory_guard:
        return set(_accepted_event_ids.get(_memory_key(state_dir), set()))


def _accepted_add(state_dir: Path | None, event_id: str) -> None:
    with _memory_guard:
        _accepted_event_ids.setdefault(_memory_key(state_dir), set()).add(event_id)


def _accepted_discard(state_dir: Path | None, event_ids: set[str]) -> None:
    if not event_ids:
        return
    key = _memory_key(state_dir)
    with _memory_guard:
        current = _accepted_event_ids.get(key)
        if current is None:
            return
        current.difference_update(event_ids)
        if not current:
            _accepted_event_ids.pop(key, None)


@contextlib.contextmanager
def _file_locked(
    state_dir: Path | None,
    *,
    path: Path,
    unsafe_code: str,
    unsafe_owner_code: str,
    unavailable_code: str,
):
    root = _dir(state_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _LedgerError("ledger_dir_unavailable") from exc
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _LedgerError(unsafe_code)
        chown_mode = config.require_chown_mode()
        if info.st_uid != 0 and chown_mode == "strict":
            raise _LedgerError(unsafe_owner_code)
        if info.st_uid != 0 and chown_mode == "warn":
            log.warning(
                "admission incident lock owner 非 root（warn 放行）：path=%s uid=%s",
                path.name,
                info.st_uid,
            )
        fcntl.flock(fd, fcntl.LOCK_EX)
    except _LedgerError:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    except OSError as exc:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise _LedgerError(unavailable_code) from exc
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _locked(state_dir: Path | None):
    with _file_locked(
        state_dir,
        path=_lock_path(state_dir),
        unsafe_code="unsafe_ledger_lock",
        unsafe_owner_code="unsafe_ledger_lock_owner",
        unavailable_code="ledger_lock_unavailable",
    ):
        yield


@contextlib.contextmanager
def _delivery_locked(state_dir: Path | None):
    with _file_locked(
        state_dir,
        path=_delivery_lock_path(state_dir),
        unsafe_code="unsafe_delivery_lock",
        unsafe_owner_code="unsafe_delivery_lock_owner",
        unavailable_code="delivery_lock_unavailable",
    ):
        yield


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "sequence": 0,
        "incident": None,
        "outbox": [],
    }


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_SAFE_INT


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _valid_incident(incident: object) -> bool:
    if incident is None:
        return True
    if not isinstance(incident, dict):
        return False
    expected = {
        "id",
        "phase",
        "revision",
        "error_code",
        "first_seen_at",
        "last_seen_at",
        "recovered_at",
        "last_effective_mode",
        "last_effective_generation",
    }
    if set(incident) != expected:
        return False
    incident_id = incident["id"]
    phase = incident["phase"]
    first_seen = incident["first_seen_at"]
    last_seen = incident["last_seen_at"]
    recovered_at = incident["recovered_at"]
    revision = incident["revision"]
    return (
        isinstance(incident_id, str)
        and re.fullmatch(r"admission-[0-9a-f]{32}", incident_id) is not None
        and isinstance(phase, str)
        and phase in {"open", "recovered"}
        and _is_nonnegative_int(revision)
        and revision >= 1
        and isinstance(incident["error_code"], str)
        and _SAFE_CODE_RE.fullmatch(incident["error_code"]) is not None
        and _is_finite_number(first_seen)
        and _is_finite_number(last_seen)
        and last_seen >= first_seen
        and (
            (phase == "open" and recovered_at is None)
            or (
                phase == "recovered"
                and _is_finite_number(recovered_at)
                and recovered_at >= last_seen
            )
        )
        and isinstance(incident["last_effective_mode"], str)
        and incident["last_effective_mode"] in _MODES
        and _is_nonnegative_int(incident["last_effective_generation"])
    )


def _valid_outbox_event(event: object) -> bool:
    if not isinstance(event, dict) or set(event) != {"event_id", "kind", "title", "payload"}:
        return False
    event_id = event["event_id"]
    kind = event["kind"]
    title = event["title"]
    payload = event["payload"]
    if (
        not isinstance(event_id, str)
        or re.fullmatch(
            r"admission-[0-9a-f]{32}:"
            r"(?:fault:[1-9][0-9]{0,18}:[a-z0-9_]{1,64}|recovered)",
            event_id,
        )
        is None
        or not isinstance(kind, str)
        or kind not in {"admission_mode_fault", "admission_mode_recovered"}
        or not isinstance(title, str)
        or title
        not in {
            "Task admission intake 已停止",
            "Task admission intake 已恢復",
        }
        or not isinstance(payload, dict)
    ):
        return False
    expected_payload = {
        "event_id",
        "incident_id",
        "error_code",
        "effective_mode",
        "effective_generation",
    }
    if kind == "admission_mode_recovered":
        expected_payload.add("duration_s")
    if set(payload) != expected_payload:
        return False
    incident_id = payload.get("incident_id")
    if (
        payload["event_id"] != event_id
        or not isinstance(incident_id, str)
        or re.fullmatch(r"admission-[0-9a-f]{32}", incident_id) is None
        or not event_id.startswith(f"{incident_id}:")
        or not isinstance(payload["error_code"], str)
        or _SAFE_CODE_RE.fullmatch(payload["error_code"]) is None
        or not isinstance(payload["effective_mode"], str)
        or payload["effective_mode"] not in _MODES
        or not _is_nonnegative_int(payload["effective_generation"])
    ):
        return False
    if kind == "admission_mode_fault":
        revision = int(event_id.split(":", 3)[2])
        return (
            revision <= _MAX_SAFE_INT
            and event_id.endswith(f":{payload['error_code']}")
            and title == "Task admission intake 已停止"
        )
    return (
        event_id.endswith(":recovered")
        and title == "Task admission intake 已恢復"
        and _is_finite_number(payload["duration_s"])
    )


def _validate_ledger(data: object) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "sequence",
        "incident",
        "outbox",
    }:
        raise _LedgerError("ledger_invalid_schema")
    if (
        data["schema_version"] != SCHEMA_VERSION
        or not _is_nonnegative_int(data["sequence"])
        or not _valid_incident(data["incident"])
        or not isinstance(data["outbox"], list)
        or len(data["outbox"]) > 128
    ):
        raise _LedgerError("ledger_invalid_schema")
    incident = data["incident"]
    if incident is None and (data["sequence"] != 0 or data["outbox"]):
        raise _LedgerError("ledger_invalid_schema")
    if isinstance(incident, dict):
        minimum_sequence = int(incident["revision"])
        if incident["phase"] == "recovered":
            minimum_sequence += 1
        if data["sequence"] < minimum_sequence:
            raise _LedgerError("ledger_invalid_schema")
    if data["outbox"] and not isinstance(incident, dict):
        raise _LedgerError("ledger_invalid_schema")
    if isinstance(incident, dict):
        event_ids: set[str] = set()
        for event in data["outbox"]:
            if not _valid_outbox_event(event) or event["event_id"] in event_ids:
                raise _LedgerError("ledger_invalid_schema")
            event_ids.add(event["event_id"])
    return data


def _read_unlocked(state_dir: Path | None) -> dict[str, Any]:
    path = _path(state_dir)
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return _empty_ledger()
    except OSError as exc:
        raise _LedgerError("ledger_unreadable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _LedgerError("unsafe_ledger_file")
        chown_mode = config.require_chown_mode()
        if info.st_uid != 0 and chown_mode == "strict":
            raise _LedgerError("unsafe_ledger_owner")
        if info.st_size > _MAX_LEDGER_BYTES:
            raise _LedgerError("ledger_too_large")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            raw = handle.read(_MAX_LEDGER_BYTES + 1)
    except _LedgerError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _LedgerError("ledger_unreadable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw.encode("utf-8")) > _MAX_LEDGER_BYTES:
        raise _LedgerError("ledger_too_large")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise _LedgerError("ledger_invalid_json") from exc
    return _validate_ledger(data)


def _write_unlocked(ledger: dict[str, Any], state_dir: Path | None) -> None:
    _validate_ledger(ledger)
    try:
        body = (
            json.dumps(
                ledger,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode()
    except (TypeError, ValueError) as exc:
        raise _LedgerError("ledger_invalid_schema") from exc
    try:
        secure_write_root(_path(state_dir), body)
    except Exception as exc:  # noqa: BLE001 — 對外只暴露穩定 diagnostic code
        raise _LedgerError("ledger_write_failed") from exc


def _event_from_dict(data: Mapping[str, Any]) -> IncidentEvent:
    return IncidentEvent(
        event_id=str(data["event_id"]),
        kind=str(data["kind"]),
        title=str(data["title"]),
        payload=dict(data["payload"]),
    )


def _projection(
    ledger: Mapping[str, Any],
    *,
    durability: str = "durable",
    diagnostic: str = "",
) -> dict[str, Any]:
    incident = ledger.get("incident")
    if not isinstance(incident, dict):
        return {
            "sequence": int(ledger.get("sequence", 0)),
            "active": False,
            "incident_id": None,
            "error_code": "",
            "first_seen_at": None,
            "last_seen_at": None,
            "duration_s": 0.0,
            "last_effective_mode": "",
            "last_effective_generation": 0,
            "recovered_at": None,
            "durability": durability,
            "diagnostic": diagnostic,
        }
    active = incident.get("phase") == "open"
    first_seen = float(incident["first_seen_at"])
    last_seen = float(incident["last_seen_at"])
    recovered_at = incident.get("recovered_at")
    observed_now = time.time()
    if not math.isfinite(observed_now) or observed_now < 0:
        observed_now = last_seen
    end = observed_now if active else float(recovered_at or last_seen)
    return {
        "sequence": int(ledger.get("sequence", 0)),
        "active": active,
        "incident_id": str(incident["id"]),
        "error_code": str(incident["error_code"]) if active else "",
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
        "duration_s": round(max(0.0, end - first_seen), 3),
        "last_effective_mode": str(incident["last_effective_mode"]),
        "last_effective_generation": int(incident["last_effective_generation"]),
        "recovered_at": float(recovered_at) if recovered_at is not None else None,
        "durability": durability,
        "diagnostic": diagnostic,
    }


def _deliver_pending_durable(
    _ledger: dict[str, Any],
    *,
    notify: NotificationSink,
    state_dir: Path | None,
) -> tuple[bool, bool, str]:
    """逐筆送出並在明確 acceptance 後 ack；delivery lock 防止並發雙送。

    外部送達後、ledger ack 前若行程終止，下一輪會以相同 event_id 重送；這是刻意的
    at-least-once 語意，接收端可依穩定 event_id 去重。
    """
    key = _memory_key(state_dir)
    active_keys = getattr(_delivery_context, "active_keys", None)
    if active_keys is None:
        active_keys = set()
        _delivery_context.active_keys = active_keys
    if key in active_keys:
        # notifier callback 若重入 observe，不可再次 flock 同一 delivery lock；外層
        # dispatcher 返回後會繼續 drain 此次新 append 的 event。
        return False, True, "notification_reentrant"

    queued = False
    accepted_snapshots: dict[str, dict[str, Any]] = {}
    active_keys.add(key)
    try:
        with _delivery_locked(state_dir):
            while True:
                with _locked(state_dir):
                    current = _read_unlocked(state_dir)
                    present_ids = {
                        str(candidate.get("event_id")) for candidate in current["outbox"]
                    }
                    accepted_ids = _accepted_get(state_dir)
                    _accepted_discard(state_dir, accepted_ids - present_ids)
                    accepted_ids &= present_ids
                    if accepted_ids:
                        current["outbox"] = [
                            candidate
                            for candidate in current["outbox"]
                            if candidate.get("event_id") not in accepted_ids
                        ]
                        _write_unlocked(current, state_dir)
                        _accepted_discard(state_dir, accepted_ids)
                    if not current["outbox"]:
                        return queued, False, ""
                    raw_event = copy.deepcopy(current["outbox"][0])
                    event_ledger = copy.deepcopy(current)
                event = _event_from_dict(raw_event)
                try:
                    accepted = notify(event)
                except Exception:  # noqa: BLE001 — outbox 保留，下一輪重試
                    log.error("task admission incident 通知 adapter 失敗（notification_failed）")
                    return queued, True, "notification_failed"
                if accepted is False:
                    log.error("task admission incident 通知未獲 sink acceptance")
                    return queued, True, "notification_rejected"
                queued = True
                _accepted_add(state_dir, event.event_id)
                accepted_snapshots[event.event_id] = event_ledger

                # 網路呼叫不持有 ledger lock；重新讀取後只 ack 同一穩定 event ID，
                # 保留期間由其他 observer append 的 lifecycle transition。
                with _locked(state_dir):
                    current = _read_unlocked(state_dir)
                    current["outbox"] = [
                        candidate
                        for candidate in current["outbox"]
                        if candidate.get("event_id") != event.event_id
                    ]
                    _write_unlocked(current, state_dir)
                _accepted_discard(state_dir, {event.event_id})
                accepted_snapshots.pop(event.event_id, None)
    except _LedgerError as exc:
        accepted_ids = _accepted_get(state_dir)
        fallback_source = next(
            (
                snapshot
                for event_id, snapshot in reversed(accepted_snapshots.items())
                if event_id in accepted_ids
            ),
            _ledger,
        )
        matched_ids = {
            str(candidate.get("event_id"))
            for candidate in fallback_source.get("outbox", [])
            if candidate.get("event_id") in accepted_ids
        }
        if matched_ids:
            # 保留 lifecycle 供 ledger 持續損壞時做同程序 dedup，但同時記住它所基於
            # 的 durable base；修復後若 disk 已前進，observe 會 reapply 而非整份覆寫。
            fallback = copy.deepcopy(fallback_source)
            fallback["outbox"] = [
                candidate
                for candidate in fallback["outbox"]
                if candidate.get("event_id") not in matched_ids
            ]
            _memory_set(
                state_dir,
                fallback,
                exc.code,
                base_ledger=fallback_source,
                latest_observation=_observation_from_ledger(fallback_source),
            )
            return queued, False, exc.code
        return queued, True, exc.code
    finally:
        active_keys.discard(key)


def _deliver_pending_memory(
    ledger: dict[str, Any],
    *,
    notify: NotificationSink,
    state_dir: Path | None,
    diagnostic: str,
) -> tuple[bool, bool, str]:
    key = _memory_key(state_dir)
    active_keys = getattr(_delivery_context, "active_keys", None)
    if active_keys is None:
        active_keys = set()
        _delivery_context.active_keys = active_keys
    if key in active_keys:
        return False, True, "notification_reentrant"

    queued = False
    active_keys.add(key)
    try:
        while True:
            memory = _memory_get(state_dir)
            if memory is None:
                # notifier 重入時可能已成功把 fallback promote 成 durable；不可用舊
                # local copy 把它重新降回 memory。
                return queued, False, diagnostic
            current = memory.ledger
            accepted_ids = _accepted_get(state_dir)
            if accepted_ids:
                current["outbox"] = [
                    candidate
                    for candidate in current["outbox"]
                    if candidate.get("event_id") not in accepted_ids
                ]
                _memory_set(state_dir, current, memory.diagnostic)
            if not current["outbox"]:
                ledger.clear()
                ledger.update(copy.deepcopy(current))
                return queued, False, diagnostic

            event = _event_from_dict(copy.deepcopy(current["outbox"][0]))
            try:
                accepted = notify(event)
            except Exception:  # noqa: BLE001 — memory outbox 留待下一輪
                log.error("task admission incident 通知 adapter 失敗（notification_failed）")
                return queued, True, "notification_failed"
            if accepted is False:
                log.error("task admission incident 通知未獲 sink acceptance")
                return queued, True, "notification_rejected"
            queued = True
            _accepted_add(state_dir, event.event_id)

            # callback 可重入 observe；重新取得最新 memory state 後只移除本 event，
            # 保留 nested transition/outbox。
            memory = _memory_get(state_dir)
            if memory is None:
                return queued, False, diagnostic
            current = memory.ledger
            current["outbox"] = [
                candidate
                for candidate in current["outbox"]
                if candidate.get("event_id") != event.event_id
            ]
            _memory_set(state_dir, current, memory.diagnostic)
            ledger.clear()
            ledger.update(copy.deepcopy(current))
    finally:
        active_keys.discard(key)


def _fault_event(observation: Faulted, incident_id: str, revision: int) -> dict[str, Any]:
    event_id = f"{incident_id}:fault:{revision}:{observation.error_code}"
    return {
        "event_id": event_id,
        "kind": "admission_mode_fault",
        "title": "Task admission intake 已停止",
        "payload": {
            "event_id": event_id,
            "incident_id": incident_id,
            "error_code": observation.error_code,
            "effective_mode": observation.effective_mode,
            "effective_generation": observation.effective_generation,
        },
    }


def _new_incident(observation: Faulted, *, now: float) -> tuple[dict[str, Any], dict[str, Any]]:
    incident_id = f"admission-{uuid.uuid4().hex}"
    incident = {
        "id": incident_id,
        "phase": "open",
        "revision": 1,
        "error_code": observation.error_code,
        "first_seen_at": now,
        "last_seen_at": now,
        "recovered_at": None,
        "last_effective_mode": observation.effective_mode,
        "last_effective_generation": observation.effective_generation,
    }
    return incident, _fault_event(observation, incident_id, 1)


def _recovery_event(
    observation: IntakeRestored,
    incident: Mapping[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    event_id = f"{incident['id']}:recovered"
    return {
        "event_id": event_id,
        "kind": "admission_mode_recovered",
        "title": "Task admission intake 已恢復",
        "payload": {
            "event_id": event_id,
            "incident_id": incident["id"],
            "error_code": incident["error_code"],
            "effective_mode": observation.effective_mode,
            "effective_generation": observation.effective_generation,
            "duration_s": round(max(0.0, now - float(incident["first_seen_at"])), 3),
        },
    }


def _phase(ledger: Mapping[str, Any]) -> tuple[str | None, Literal["none", "open", "recovered"]]:
    incident = ledger.get("incident")
    if not isinstance(incident, dict):
        return None, "none"
    incident_id = str(incident["id"])
    return incident_id, "open" if incident.get("phase") == "open" else "recovered"


def _observation_from_ledger(ledger: Mapping[str, Any]) -> AdmissionObservation | None:
    incident = ledger.get("incident")
    if not isinstance(incident, dict):
        return None
    mode = str(incident["last_effective_mode"])
    generation = int(incident["last_effective_generation"])
    if incident.get("phase") == "open":
        return Faulted(str(incident["error_code"]), mode, generation)
    return IntakeRestored(mode, generation)


def _normalized_observation(observation: AdmissionObservation) -> AdmissionObservation:
    """只允許穩定 code/mode/generation 進 ledger、heartbeat 與外部通知。"""
    if isinstance(observation, Waiting):
        return Waiting()
    mode = (
        observation.effective_mode
        if isinstance(observation.effective_mode, str) and observation.effective_mode in _MODES
        else "shadow"
    )
    generation = observation.effective_generation
    if not _is_nonnegative_int(generation):
        generation = 0
    if isinstance(observation, IntakeRestored):
        return IntakeRestored(mode, generation)
    error_code = (
        observation.error_code
        if isinstance(observation.error_code, str)
        and _SAFE_CODE_RE.fullmatch(observation.error_code)
        else "unknown_fault"
    )
    return Faulted(error_code, mode, generation)


def _apply_observation(
    ledger: dict[str, Any],
    observation: AdmissionObservation,
    *,
    now: float,
) -> bool:
    """純 transition；回 True 代表 ledger 有變更。"""
    incident = ledger.get("incident")
    if isinstance(observation, Waiting):
        return False
    if isinstance(observation, Faulted):
        if not isinstance(incident, dict) or incident.get("phase") != "open":
            created, event = _new_incident(observation, now=now)
            ledger["sequence"] = int(ledger.get("sequence", 0)) + 1
            ledger["incident"] = created
            ledger["outbox"].append(event)
            return True
        observed_at = max(now, float(incident["last_seen_at"]))
        ledger["sequence"] = int(ledger.get("sequence", 0)) + 1
        changed_code = incident.get("error_code") != observation.error_code
        incident["last_seen_at"] = observed_at
        incident["last_effective_mode"] = observation.effective_mode
        incident["last_effective_generation"] = observation.effective_generation
        if changed_code:
            incident["revision"] = int(incident.get("revision", 1)) + 1
            incident["error_code"] = observation.error_code
            ledger["outbox"].append(
                _fault_event(observation, str(incident["id"]), int(incident["revision"]))
            )
        return True
    if not isinstance(incident, dict) or incident.get("phase") != "open":
        return False
    observed_at = max(now, float(incident["last_seen_at"]))
    ledger["sequence"] = int(ledger.get("sequence", 0)) + 1
    ledger["outbox"].append(_recovery_event(observation, incident, now=observed_at))
    incident["phase"] = "recovered"
    incident["last_seen_at"] = observed_at
    incident["recovered_at"] = observed_at
    incident["last_effective_mode"] = observation.effective_mode
    incident["last_effective_generation"] = observation.effective_generation
    return True


def _same_lifecycle(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Outbox ack 不算 lifecycle 前進；sequence + incident 才是 fallback base。"""
    return (
        left.get("schema_version") == right.get("schema_version")
        and left.get("sequence") == right.get("sequence")
        and left.get("incident") == right.get("incident")
    )


def _empty_lifecycle(ledger: Mapping[str, Any]) -> bool:
    return ledger.get("sequence") == 0 and ledger.get("incident") is None


def _merge_outbox(
    primary: dict[str, Any],
    secondary: Mapping[str, Any],
) -> dict[str, Any]:
    """保留 queue 首次位置，但同 event ID 的 payload 以 primary lifecycle 為準。"""
    merged = copy.deepcopy(primary)
    ordered_ids: list[str] = []
    events: dict[str, dict[str, Any]] = {}
    for candidate in [*secondary.get("outbox", []), *primary.get("outbox", [])]:
        event_id = candidate.get("event_id")
        if not isinstance(event_id, str):
            continue
        if event_id not in events:
            ordered_ids.append(event_id)
        events[event_id] = copy.deepcopy(candidate)
    merged["outbox"] = [events[event_id] for event_id in ordered_ids]
    return merged


def _memory_is_based_on_disk(memory: _MemoryLedger, disk: Mapping[str, Any]) -> bool:
    if _empty_lifecycle(disk):
        # Runbook quarantine 後以仍存活的 process-local lifecycle 恢復，不另開 incident。
        return True
    return memory.base_ledger is not None and _same_lifecycle(memory.base_ledger, disk)


def _latest_observation(
    current: AdmissionObservation,
    previous: AdmissionObservation | None,
) -> AdmissionObservation | None:
    return previous if isinstance(current, Waiting) else current


def _observe_impl(
    observation: AdmissionObservation,
    *,
    notify: NotificationSink,
    state_dir: Path | None = None,
) -> IncidentReceipt:
    """記錄 worker 最終觀察值；incident/通知故障不得向 caller 冒泡。"""
    observation = _normalized_observation(observation)
    now = time.time()
    if not math.isfinite(now) or now < 0:
        now = 0.0
    durable = False
    diagnostic = ""
    ledger = _empty_ledger()
    transition_evaluated = False
    base_for_failure: dict[str, Any] | None = None
    latest_for_failure: AdmissionObservation | None = (
        None if isinstance(observation, Waiting) else observation
    )
    try:
        with _locked(state_dir):
            memory = _memory_get(state_dir)
            disk = _read_unlocked(state_dir)
            base_for_failure = copy.deepcopy(disk)
            if memory is None:
                ledger = disk
                changed = _apply_observation(ledger, observation, now=now)
            elif _memory_is_based_on_disk(memory, disk):
                # Disk 仍是 fallback 的 base（或已由 runbook quarantine 成空）：
                # process-local lifecycle 是同一 causal branch，可直接 promote。
                ledger = _merge_outbox(memory.ledger, disk)
                changed = _apply_observation(ledger, observation, now=now)
                latest_for_failure = _latest_observation(
                    observation,
                    memory.latest_observation,
                )
            else:
                # Disk 已從 base 前進：絕不以較大的 scalar sequence 覆寫。以 disk
                # lifecycle 為主，再套 worker 最新 truth；舊 memory 只貢獻 pending outbox。
                ledger = _merge_outbox(disk, memory.ledger)
                # Waiting 不宣稱 intake healthy/faulted，不能拿舊 memory observation
                # 關閉或覆寫另一程序已前進的 disk incident。
                replay = None if isinstance(observation, Waiting) else observation
                changed = replay is not None and _apply_observation(
                    ledger,
                    replay,
                    now=now,
                )
                latest_for_failure = replay or _observation_from_ledger(ledger)
            transition_evaluated = True
            if changed or memory is not None:
                _write_unlocked(ledger, state_dir)
        durable = True
        _memory_clear(state_dir)
    except _LedgerError as exc:
        diagnostic = exc.code
        if not transition_evaluated:
            memory = _memory_get(state_dir)
            if memory is not None:
                ledger = memory.ledger
                base_for_failure = memory.base_ledger
                latest_for_failure = _latest_observation(
                    observation,
                    memory.latest_observation,
                )
            _apply_observation(ledger, observation, now=now)
        _memory_set(
            state_dir,
            ledger,
            diagnostic,
            base_ledger=base_for_failure,
            latest_observation=latest_for_failure,
        )
        log.error("task admission incident ledger 失敗（%s），改用行程記憶體", diagnostic)

    incident_id, phase = _phase(ledger)
    if durable:
        queued, deferred, delivery_diagnostic = _deliver_pending_durable(
            ledger,
            notify=notify,
            state_dir=state_dir,
        )
        memory_after_delivery = _memory_get(state_dir)
        durability: Literal["durable", "memory_only"] = (
            "memory_only" if memory_after_delivery is not None else "durable"
        )
        if memory_after_delivery is not None:
            diagnostic = memory_after_delivery.diagnostic
        elif delivery_diagnostic:
            diagnostic = delivery_diagnostic
    else:
        queued, deferred, delivery_diagnostic = _deliver_pending_memory(
            ledger,
            notify=notify,
            state_dir=state_dir,
            diagnostic=diagnostic,
        )
        durability = "memory_only"
        if delivery_diagnostic.startswith("notification_"):
            diagnostic = delivery_diagnostic

    if deferred:
        notification: Literal["queued", "deduped", "deferred"] = "deferred"
    else:
        notification = "queued" if queued else "deduped"
    return IncidentReceipt(
        incident_id,
        phase,
        durability,
        notification,
        diagnostic,
    )


def observe(
    observation: AdmissionObservation,
    *,
    notify: NotificationSink,
    state_dir: Path | None = None,
) -> IncidentReceipt:
    """永不拋錯的 mutation seam；所有預期 ledger/adapter fault 由內部降級處理。"""
    try:
        with _process_locked(state_dir):
            return _observe_impl(observation, notify=notify, state_dir=state_dir)
    except Exception:  # noqa: BLE001 — observability 失敗不得改寫 worker intake 判定
        log.exception("task admission incident observe 發生未預期錯誤")
        return IncidentReceipt(
            None,
            "none",
            "memory_only",
            "deferred",
            "incident_internal_error",
        )


def snapshot(*, state_dir: Path | None = None) -> dict[str, Any]:
    """回 operator-safe incident 投影；ledger 壞損時也永不拋錯。"""
    try:
        with _process_locked(state_dir):
            memory = _memory_get(state_dir)
            try:
                with _locked(state_dir):
                    disk = _read_unlocked(state_dir)
            except _LedgerError:
                if memory is not None:
                    return _projection(
                        memory.ledger,
                        durability="memory_only",
                        diagnostic=memory.diagnostic,
                    )
                raise
            if memory is not None and _memory_is_based_on_disk(memory, disk):
                return _projection(
                    memory.ledger,
                    durability="memory_only",
                    diagnostic=memory.diagnostic,
                )
            return _projection(disk)
    except _LedgerError as exc:
        return _projection(_empty_ledger(), durability="memory_only", diagnostic=exc.code)
    except Exception:  # noqa: BLE001 — read seam 不得讓 API/worker 因 observability 崩潰
        log.exception("task admission incident snapshot 發生未預期錯誤")
        return _projection(
            _empty_ledger(),
            durability="memory_only",
            diagnostic="ledger_internal_error",
        )
