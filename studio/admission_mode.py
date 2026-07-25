"""跨程序 task-admission mode 握手。

Web 與 autopilot 是獨立行程，不能把各自行程內的環境變數當成同一份 runtime
狀態。本模組把 desired/effective mode、generation、跨程序鎖與
release-before-ack 順序藏在單一介面後：

* :func:`bootstrap_at_task_boundary` 只允許 worker 用已知 effective 建立遺失狀態。
* :func:`request` 只更新已存在狀態的 desired generation。
* :func:`snapshot` 提供 lock-free、原子檔案快照；壞檔只回去敏 fault。
* :func:`reconcile_at_task_boundary` 只能由 worker 在無任務執行的邊界呼叫，
  降級時先釋放 admission holds，成功後才 ack effective generation。

狀態檔使用既有 ``secure_write_root`` 原子取代；正式環境與測試都走同一份檔案
implementation，測試只需把 ``state_dir`` 指到 ``tmp_path``。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, secure_write

log = logging.getLogger("ti.admission_mode")

SCHEMA_VERSION = 1
MODES = ("off", "shadow", "enforce")
_MAX_STATE_BYTES = 64 * 1024

# 唯一寫入 choke point；保留 module-level alias 供 fault-injection 測試。
secure_write_root = secure_write.secure_write_root


class AdmissionModeError(RuntimeError):
    """mode control state 無法安全讀寫或轉換。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ModeState:
    """desired/effective 的公開、不可變快照。"""

    desired: str
    effective: str
    generation: int
    effective_generation: int
    requested_at: float
    applied_at: float
    released_holds: int = 0
    healthy: bool = True
    error: str = ""

    @property
    def pending(self) -> bool:
        return self.generation != self.effective_generation

    def to_public(self) -> dict[str, Any]:
        """只投影 operator 需要的安全欄位，不暴露路徑或例外訊息。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "desired": self.desired,
            "effective": self.effective,
            "generation": self.generation,
            "effective_generation": self.effective_generation,
            "pending": self.pending,
            "healthy": self.healthy,
            "error": self.error,
            "requested_at": self.requested_at,
            "applied_at": self.applied_at,
            "released_holds": self.released_holds,
        }


def _dir(state_dir: Path | None) -> Path:
    return Path(state_dir) if state_dir is not None else config.AUTOPILOT_STATE_DIR


def _path(state_dir: Path | None) -> Path:
    return _dir(state_dir) / "admission_mode.json"


def _lock_path(state_dir: Path | None) -> Path:
    return _dir(state_dir) / "admission_mode.lock"


@contextlib.contextmanager
def _locked(state_dir: Path | None):
    root = _dir(state_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AdmissionModeError("state_dir_unavailable") from exc
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    try:
        fd = os.open(_lock_path(state_dir), flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AdmissionModeError("unsafe_lock_file")
        chown_mode = config.require_chown_mode()
        if info.st_uid != 0 and chown_mode == "strict":
            raise AdmissionModeError("unsafe_lock_owner")
        if info.st_uid != 0 and chown_mode == "warn":
            log.warning("admission mode lock owner 非 root（warn 放行）：uid=%s", info.st_uid)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except AdmissionModeError:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    except OSError as exc:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise AdmissionModeError("lock_unavailable") from exc
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode not in MODES:
        raise AdmissionModeError("invalid_mode")
    return mode


def _integer(value: Any, *, code: str) -> int:
    if isinstance(value, bool):
        raise AdmissionModeError(code)
    if isinstance(value, float) and not math.isfinite(value):
        raise AdmissionModeError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AdmissionModeError(code) from exc
    if parsed < 1:
        raise AdmissionModeError(code)
    return parsed


def _timestamp(value: Any, *, code: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AdmissionModeError(code) from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise AdmissionModeError(code)
    return parsed


def _decode(data: Any) -> ModeState:
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise AdmissionModeError("invalid_schema")
    desired = _mode(data.get("desired"))
    effective = _mode(data.get("effective"))
    generation = _integer(data.get("generation"), code="invalid_generation")
    effective_generation = _integer(
        data.get("effective_generation"),
        code="invalid_effective_generation",
    )
    if effective_generation > generation:
        raise AdmissionModeError("generation_regressed")
    if effective_generation == generation and effective != desired:
        raise AdmissionModeError("inconsistent_ack")
    released_holds = data.get("released_holds", 0)
    if isinstance(released_holds, bool):
        raise AdmissionModeError("invalid_released_holds")
    if isinstance(released_holds, float) and not math.isfinite(released_holds):
        raise AdmissionModeError("invalid_released_holds")
    try:
        released_holds = int(released_holds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AdmissionModeError("invalid_released_holds") from exc
    if released_holds < 0:
        raise AdmissionModeError("invalid_released_holds")
    return ModeState(
        desired=desired,
        effective=effective,
        generation=generation,
        effective_generation=effective_generation,
        requested_at=_timestamp(data.get("requested_at", 0), code="invalid_requested_at"),
        applied_at=_timestamp(data.get("applied_at", 0), code="invalid_applied_at"),
        released_holds=released_holds,
    )


def _read_unlocked(state_dir: Path | None) -> ModeState:
    path = _path(state_dir)
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise AdmissionModeError("not_initialized") from exc
    except OSError as exc:
        raise AdmissionModeError("state_unreadable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AdmissionModeError("unsafe_state_file")
        chown_mode = config.require_chown_mode()
        if info.st_uid != 0 and chown_mode == "strict":
            raise AdmissionModeError("unsafe_state_owner")
        if info.st_uid != 0 and chown_mode == "warn":
            log.warning("admission mode state owner 非 root（warn 放行）：uid=%s", info.st_uid)
        if info.st_size > _MAX_STATE_BYTES:
            raise AdmissionModeError("state_too_large")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            raw = handle.read(_MAX_STATE_BYTES + 1)
    except AdmissionModeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise AdmissionModeError("state_unreadable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw.encode("utf-8")) > _MAX_STATE_BYTES:
        raise AdmissionModeError("state_too_large")
    try:
        return _decode(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise AdmissionModeError("invalid_json") from exc


def _encode(state: ModeState) -> bytes:
    body = {
        "schema_version": SCHEMA_VERSION,
        "desired": state.desired,
        "effective": state.effective,
        "generation": state.generation,
        "effective_generation": state.effective_generation,
        "requested_at": state.requested_at,
        "applied_at": state.applied_at,
        "released_holds": state.released_holds,
    }
    return (json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _write_unlocked(state: ModeState, state_dir: Path | None) -> None:
    try:
        secure_write_root(_path(state_dir), _encode(state))
    except Exception as exc:  # noqa: BLE001 — 對外只回穩定、去敏 error code
        raise AdmissionModeError("state_write_failed") from exc


def _commit_barrier(state_dir: Path | None) -> None:
    """等待已通過舊 generation 驗證的 backlog commit；固定 lock order=mode→backlog。"""
    try:
        # 延遲 import 避免 admission_mode ↔ backlog 的 module import cycle。
        from . import backlog

        backlog.admission_commit_barrier(state_dir=state_dir)
    except Exception as exc:  # noqa: BLE001 — barrier 失敗不可宣稱 request/ack 已線性化
        raise AdmissionModeError("commit_barrier_failed") from exc


def _release_hold_count(release_holds: Callable[[str], int], mode: str) -> int:
    """執行冪等 hold release，並把 adapter 例外／壞回傳收斂成穩定錯誤碼。"""
    try:
        released_holds = release_holds(mode)
    except Exception as exc:  # noqa: BLE001 — release 失敗不可 ack／bootstrap
        raise AdmissionModeError("hold_release_failed") from exc
    if isinstance(released_holds, bool):
        raise AdmissionModeError("invalid_release_count")
    try:
        released_holds = int(released_holds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AdmissionModeError("invalid_release_count") from exc
    if released_holds < 0:
        raise AdmissionModeError("invalid_release_count")
    return released_holds


def snapshot(
    *,
    state_dir: Path | None = None,
    fallback_mode: str = "shadow",
) -> ModeState:
    """讀目前 mode state；故障時回 healthy=false，不猜測或修改持久狀態。"""
    try:
        return _read_unlocked(state_dir)
    except AdmissionModeError as exc:
        try:
            fallback = _mode(fallback_mode)
        except AdmissionModeError:
            fallback = "shadow"
        return ModeState(
            desired=fallback,
            effective=fallback,
            generation=0,
            effective_generation=0,
            requested_at=0.0,
            applied_at=0.0,
            healthy=False,
            error=exc.code,
        )


def _request_unlocked(
    desired: str,
    current: ModeState,
    state_dir: Path | None,
) -> ModeState:
    """在 mode lock 內更新既有 desired，並線性化舊 generation commit。"""
    if current.desired == desired:
        if current.pending:
            _commit_barrier(state_dir)
        return current
    updated = ModeState(
        desired=desired,
        effective=current.effective,
        generation=current.generation + 1,
        effective_generation=current.effective_generation,
        requested_at=time.time(),
        applied_at=current.applied_at,
        released_holds=current.released_holds,
    )
    _write_unlocked(updated, state_dir)
    _commit_barrier(state_dir)
    return updated


def bootstrap_at_task_boundary(
    desired_mode: str,
    *,
    initial_effective: str,
    release_holds: Callable[[str], int],
    state_dir: Path | None = None,
) -> ModeState:
    """由 worker 在無任務執行的邊界建立狀態；已有狀態時原樣讀取。

    一般 web／排程 producer 不得呼叫本函式。控制檔在 worker 執行期間遺失時，
    只有 worker 知道目前 pin 住的 effective，才能安全重建而不偷渡 mode 切換。
    process-local config 只作首次 bootstrap 預設，不得在 worker restart 覆寫 shared
    desired；既有 desired 只能由 :func:`request` 變更。
    """
    desired = _mode(desired_mode)
    initial = _mode(initial_effective)
    with _locked(state_dir):
        try:
            current = _read_unlocked(state_dir)
        except AdmissionModeError as exc:
            if exc.code != "not_initialized":
                raise
            now = time.time()
            generation = 1 if desired == initial else 2
            current = ModeState(
                desired=desired,
                effective=initial,
                generation=generation,
                effective_generation=1,
                requested_at=now,
                applied_at=now,
            )
            # missing control state 可能是首次升級，也可能是執行期刪檔後重啟。先排空
            # 已開始的 backlog commit；若重建成 legacy mode，再做一次冪等 hold
            # release，避免舊 enforce parked task 永久遺留。
            _commit_barrier(state_dir)
            if initial in {"off", "shadow"}:
                released_holds = _release_hold_count(release_holds, initial)
                current = ModeState(
                    desired=current.desired,
                    effective=current.effective,
                    generation=current.generation,
                    effective_generation=current.effective_generation,
                    requested_at=current.requested_at,
                    applied_at=current.applied_at,
                    released_holds=released_holds,
                )
            _write_unlocked(current, state_dir)
            return current
        return current


def request(
    desired_mode: str,
    *,
    state_dir: Path | None = None,
) -> ModeState:
    """原子更新既有 desired generation；未初始化時 fail closed。"""
    desired = _mode(desired_mode)
    with _locked(state_dir):
        current = _read_unlocked(state_dir)
        return _request_unlocked(desired, current, state_dir)


def reconcile_at_task_boundary(
    *,
    release_holds: Callable[[str], int],
    state_dir: Path | None = None,
) -> ModeState:
    """在 worker 無任務執行的邊界 ack；先封住舊 commit，降級再 release。"""
    with _locked(state_dir):
        current = _read_unlocked(state_dir)
        if not current.pending:
            return current
        _commit_barrier(state_dir)
        released_holds = 0
        if current.effective == "enforce" and current.desired in {"off", "shadow"}:
            released_holds = _release_hold_count(release_holds, current.desired)
        applied = ModeState(
            desired=current.desired,
            effective=current.desired,
            generation=current.generation,
            effective_generation=current.generation,
            requested_at=current.requested_at,
            applied_at=time.time(),
            released_holds=released_holds,
        )
        _write_unlocked(applied, state_dir)
        return applied
