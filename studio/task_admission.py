"""任務契約閘門。

``evaluate`` 是不讀寫檔案、不呼叫網路或模型的純決策核心；同模組中的協調函式
負責本機證據、CAS、去敏 audit、cache 與可選的單次語意 fallback。
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import re
import shlex
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTRACT_VERSION = 1
CONTRACT_KINDS = frozenset({"implementation", "investigation", "docs", "ops"})
# 只有直接建立單一 task 的來源才需要追問；intent/schedule 都是自治 emitter，
# 即使來源順位較高也不得把澄清責任丟回人類。
_HUMAN_SOURCES = frozenset({"human", "manual", "user"})
_QUALITY_RULES = frozenset(
    {
        "contract_fields_missing",
        "contract_schema_invalid",
        "target_not_found",
        "acceptance_evidence_missing",
    }
)
_ADMISSION_CIRCUIT_LATCH: dict[str, dict[str, Any]] = {}
_SEMANTIC_MEMORY_CACHE: dict[str, dict[str, Any]] = {}
_SEMANTIC_CACHE_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


@dataclass(frozen=True, slots=True)
class TaskContract:
    """正規化後的 v1 任務契約。"""

    version: int
    outcome: str
    kind: str
    targets: tuple[str, ...]
    acceptance: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    external_writes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """回傳只含 JSON 型別的契約。"""
        return {
            "version": self.version,
            "outcome": self.outcome,
            "kind": self.kind,
            "targets": list(self.targets),
            "acceptance": list(self.acceptance),
            "constraints": list(self.constraints),
            "external_writes": list(self.external_writes),
        }


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """``evaluate`` 的公開裁決結果。"""

    outcome: str
    contract: TaskContract
    reasons: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    unresolved_targets: tuple[str, ...] = ()
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """回傳可直接交給 JSON encoder 的表示。"""
        return {
            "outcome": self.outcome,
            "contract": self.contract.to_dict(),
            "reasons": list(self.reasons),
            "missing_fields": list(self.missing_fields),
            "unresolved_targets": list(self.unresolved_targets),
            "audit": dict(self.audit),
        }


@dataclass(frozen=True, slots=True)
class AdmissionSelection:
    """Claim-time 協調結果；task 的 attempts 已還原成認領前值供既有 runner 使用。"""

    task: dict[str, Any]
    decision: AdmissionDecision


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = ()
    return tuple(text for item in values if (text := str(item).strip()))


def _acceptance(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        values = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    elif isinstance(value, str):
        values = (value,)
    else:
        values = ()

    normalized: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            if item.get("command"):
                text = f"command: {item['command']}"
            else:
                text = str(
                    item.get("check") or item.get("evidence") or item.get("description") or ""
                )
        else:
            text = str(item)
        if text := text.strip():
            normalized.append(text)
    return tuple(normalized)


def _version(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _legacy_kind(task: Mapping[str, Any]) -> str:
    explicit = str(task.get("kind") or task.get("task_kind") or "").strip().lower()
    if explicit:
        return explicit
    title = str(task.get("title") or "")
    text = f"{title}\n{task.get('detail') or ''}"
    if re.search(
        r"(?:實作|修復|新增|修改|重構|移除|補(?:上|強)?|implement|fix|refactor)", title, re.I
    ):
        return "implementation"
    if re.search(
        r"(?:調查|盤點|彙整|分析|驗證|釐清|根因|investigat|research|audit)",
        text,
        re.I,
    ):
        return "investigation"
    legacy_type = str(task.get("type") or "").strip().lower()
    if legacy_type in {"feature", "bug", "improvement"}:
        return "implementation"
    return ""


def _contract(task: Mapping[str, Any]) -> TaskContract:
    raw = task.get("contract")
    data = raw if isinstance(raw, Mapping) else {}
    return TaskContract(
        version=_version(data.get("version", CONTRACT_VERSION)),
        outcome=str(
            data.get("outcome")
            or task.get("desired_outcome")
            or task.get("outcome")
            or task.get("detail")
            or task.get("title")
            or ""
        ).strip(),
        kind=str(data.get("kind") or _legacy_kind(task)).strip().lower(),
        targets=_strings(data.get("targets") or task.get("targets") or task.get("target")),
        acceptance=_acceptance(
            data.get("acceptance")
            or task.get("acceptance")
            or task.get("acceptance_criteria")
            or task.get("checks")
        ),
        constraints=_strings(data.get("constraints") or task.get("constraints")),
        external_writes=_strings(data.get("external_writes") or task.get("external_writes")),
    )


def _same_title_in(
    task: Mapping[str, Any],
    rows: Any,
    *,
    statuses: frozenset[str] | None = None,
) -> bool:
    title = " ".join(str(task.get("title") or "").split()).casefold()
    if not title or not isinstance(rows, (list, tuple, set, frozenset)):
        return False
    task_id = task.get("id")
    for row in rows:
        if isinstance(row, Mapping):
            if task_id is not None and str(row.get("id")) == str(task_id):
                continue
            if statuses is not None:
                status = str(row.get("status") or "")
                if status and status not in statuses:
                    continue
            candidate = row.get("title")
        else:
            candidate = row
        if " ".join(str(candidate or "").split()).casefold() == title:
            return True
    return False


def _external_writes_authorized(
    contract: TaskContract,
    context: Mapping[str, Any],
) -> bool:
    if not contract.external_writes:
        return True
    if context.get("external_write_allowed") is True:
        return True
    allowed = set(_strings(context.get("authorized_external_writes")))
    return "*" in allowed or set(contract.external_writes).issubset(allowed)


def _risk_authorized(task: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    risk = str(task.get("risk") or "unknown").strip().lower().replace("_", "-")
    if risk in {"low", "medium"}:
        return True
    if risk == "irreversible":
        return task.get("human_approved") is True or context.get("human_approved") is True
    allowed = set(_strings(context.get("authorized_risks")))
    return context.get("risk_authorized") is True or risk in allowed or "*" in allowed


def _has_external_mutation_intent(
    task: Mapping[str, Any],
    contract: TaskContract,
) -> bool:
    """由需求文字辨識明確外部變更，避免靠空 external_writes 自我宣告成安全。"""
    title = str(task.get("title") or "")
    detail = str(task.get("detail") or "")
    request_text = f"{title}\n{detail}"
    text = "\n".join((title, detail, contract.outcome, *contract.constraints))
    external_target = any(_is_external_target(target) for target in contract.targets)
    # v1 沒有結構化 read-only effect；外部 target 的 ops 因此必須顯式宣告
    # external_writes。純讀工作應使用 investigation，避免靠自由文字動詞猜測放行。
    if contract.kind == "ops" and external_target:
        return True
    english_action = (
        r"(?:deploy|publish|release|merge|push|upload|sync|rollout|restart|delete|drop|"
        r"truncate|destroy|purge|provision|promote|ship|apply|update|edit|create|close|"
        r"comment|post|send)"
    )
    english_object = (
        r"(?:prod(?:uction)?|remote|github|cloud|database|db|server|cluster|service|"
        r"registry|package|branch|pull\s+request|pr|environment)"
    )
    chinese_action = (
        r"(?:部署|發佈|發布|上線|合併|推送|上傳|同步|重啟|刪除|清空|銷毀|下架|套用|"
        r"更新|修改|建立|關閉|新增|留言|傳送)"
    )
    chinese_object = r"(?:正式|生產|遠端|雲端|資料庫|伺服器|服務|叢集|套件|分支|環境|\bPR\b)"
    paired = re.search(
        rf"\b{english_action}\b.{{0,80}}\b{english_object}\b|"
        rf"\b{english_object}\b.{{0,80}}\b{english_action}\b|"
        rf"{chinese_action}.{{0,40}}{chinese_object}|{chinese_object}.{{0,40}}{chinese_action}",
        text,
        re.I | re.S,
    )
    request_paired = re.search(
        rf"\b{english_action}\b.{{0,80}}\b{english_object}\b|"
        rf"\b{english_object}\b.{{0,80}}\b{english_action}\b|"
        rf"{chinese_action}.{{0,40}}{chinese_object}|{chinese_object}.{{0,40}}{chinese_action}",
        request_text,
        re.I | re.S,
    )
    direct_title = re.match(
        rf"\s*(?:{english_action}\b|{chinese_action})",
        title,
        re.I,
    )
    # deploy/publish/send 等動詞本身就是 repo 外效果；不能靠把 kind/target 填成本機
    # implementation 來隱藏。逐 clause 判斷，只有前面已明示「修 code／測試／調查」
    # 的語境才把它當成被談論的能力，避免「修復 deploy client」這類本機工作被誤擋。
    outbound_english = (
        r"(?:(?:re-?)?deploy(?:ed|ing)?|(?:re-?)?publish(?:ed|ing)?|"
        r"releas(?:e|ed|ing)|"
        r"merg(?:e|ed|ing)|push(?:ed|ing)?|upload(?:ed|ing)?|sync(?:ed|ing)?|"
        r"rollout|restart(?:ed|ing)?|provision(?:ed|ing)?|promot(?:e|ed|ing)|"
        r"ship(?:ped|ping)?|comment(?:ed|ing)?|post(?:ed|ing)?|"
        r"(?:re-?)?(?:send|sent)|"
        r"email(?:ed|ing)?|forward(?:ed|ing)?|repl(?:y|ied|ying)|"
        r"respond(?:ed|ing)?|notif(?:y|ied|ying)|messag(?:e|ed|ing))"
    )
    outbound_chinese = (
        r"(?:重新部署|部署|發佈|發布|上線|合併|推送|上傳|同步|重啟|留言|"
        r"傳送|發送|寄送|寄出|通知|回覆)"
    )
    outbound_pattern = re.compile(
        rf"\b{outbound_english}\b|{outbound_chinese}",
        re.I,
    )
    local_context = re.compile(
        r"\b(?:fix|implement|add|update|improve|harden|optimi[sz]e|rename|review|"
        r"support|build|write|create|refactor|test|document|investigate|analy[sz]e|"
        r"audit|prevent|disable|mock|show|display|render)\b|修復|實作|新增|加入|"
        r"更新|改善|強化|優化|顯示|呈現|"
        r"重新命名|檢視|支援|建立|撰寫|重構|測試|記錄|文件|調查|分析|稽核|防止|停用",
        re.I,
    )
    negated = re.compile(
        r"(?:\b(?:do\s+not|don'?t|never|avoid)\s*|不要|不得|禁止|請勿|避免)\s*$",
        re.I,
    )
    clause_boundary = re.compile(
        r"\b(?:and|then|after|before)\b|[,，/:：;.!?。！？]|完成後|之後|然後|接著|"
        r"並且|再|並|後"
    )
    capability_noun = re.compile(
        r"^[\s_-]*(?:client|helper|tool|module|logic|button|command|script|test|workflow|"
        r"code|parser|adapter|function|客戶端|工具|模組|邏輯|按鈕|命令|腳本|測試|"
        r"流程|程式)",
        re.I,
    )
    local_artifact = re.compile(
        r"\b(?:retr(?:y|ies)|logic|test|code|module|button|command|script|parser|adapter|"
        r"function|status|dashboard|mock|local(?:ly)?|simulat(?:e|ed|ion))\b|"
        r"重試|邏輯|測試|程式|模組|按鈕|命令|腳本|解析器|狀態|儀表板|模擬|本機",
        re.I,
    )
    outbound_request = False
    title_negates_detail = negated.search(title.strip()) is not None
    guardrail_context = bool(
        local_context.search(title)
        and re.search(r"\b(?:guardrail|approval|policy|check)\b|防護|護欄|核准|政策", title, re.I)
    )
    for line_index, line in enumerate(request_text.splitlines()):
        for match in outbound_pattern.finditer(line):
            prefix = line[: match.start()]
            suffix = line[match.end() :]
            clause = clause_boundary.split(prefix)[-1]
            if negated.search(clause) or (line_index > 0 and title_negates_detail):
                continue
            if local_context.search(clause):
                continue
            if guardrail_context and re.search(
                r"\bonly\s+(?:after|if|when)\b|僅在|只有.{0,20}才|核准後",
                suffix,
                re.I,
            ):
                continue
            if capability_noun.search(suffix) and (
                local_context.search(f"{prefix} {suffix}") or local_artifact.search(suffix)
            ):
                continue
            outbound_request = True
            break
        if outbound_request:
            break
    explicit_external_pair = re.search(
        r"\b(?:open|reopen|raise|submit|approve|file|create|close|remove|delete|assign|"
        r"request\s+(?:a\s+)?review|add|set|change|edit|update|label|star|fork|"
        r"invite|cancel|rerun|run|scale|rotate|call|trigger|dispatch|comment|merge)"
        r"\b.{0,60}\b(?:pull\s+request|pr|github\s+issue|issue|github\s+repository|"
        r"github\s+repo|repository|github\s+workflow|production\s+cluster|"
        r"production\s+dns(?:\s+record)?|production\s+secret|customer\s+webhook)\b|"
        r"(?:建立|開啟|重開|提交|核准|關閉|移除|刪除|指派|要求審查|新增|設定|"
        r"變更|更新|標記|邀請|取消|重跑|擴縮|輪替|呼叫|觸發).{0,40}"
        r"(?:PR|pull request|GitHub issue|議題|GitHub repository|GitHub workflow|"
        r"正式叢集|生產叢集|正式 DNS|生產密鑰|客戶 webhook)",
        request_text,
        re.I | re.S,
    )
    deployment_request = re.search(
        r"\b(?:run|perform|schedule|start|trigger)\s+(?:a\s+)?deployment\b.{0,80}"
        r"\b(?:prod(?:uction)?|remote|web\s+app|site|service)\b|"
        r"(?:執行|安排|啟動|觸發).{0,20}部署.{0,40}(?:正式|生產|遠端|網站|服務)",
        request_text,
        re.I | re.S,
    )

    def actionable_phrase(match: re.Match[str] | None) -> bool:
        if match is None:
            return False
        line_start = request_text.rfind("\n", 0, match.start()) + 1
        line_end = request_text.find("\n", match.end())
        if line_end < 0:
            line_end = len(request_text)
        clause = clause_boundary.split(request_text[line_start : match.start()])[-1]
        effect_clause = clause_boundary.split(request_text[match.start() : line_end])[0]
        return (
            not (line_start > 0 and title_negates_detail)
            and not negated.search(clause)
            and not local_context.search(clause)
            and not local_artifact.search(effect_clause)
        )

    direct_effect_clause = clause_boundary.split(title)[0]
    direct_suffix = title[direct_title.end() :] if direct_title else ""
    direct_is_local_artifact = bool(
        local_artifact.search(direct_effect_clause)
        or (capability_noun.search(direct_suffix) and local_artifact.search(title))
    )
    return bool(
        outbound_request
        or actionable_phrase(request_paired)
        or actionable_phrase(explicit_external_pair)
        or actionable_phrase(deployment_request)
        or (paired and (contract.kind == "ops" or external_target))
        or (
            direct_title
            and not direct_is_local_artifact
            and (
                contract.kind == "ops"
                or external_target
                or re.search(rf"\b{english_object}\b|{chinese_object}", text, re.I)
            )
        )
    )


def _is_external_target(target: str) -> bool:
    """所有 URI/resource scheme 都視為外部；contract 的本機 target 只能是 repo 相對路徑。"""
    return re.match(r"^[a-z][a-z0-9+.-]*:", target.strip(), re.I) is not None


def _has_irreversible_intent(
    task: Mapping[str, Any],
    contract: TaskContract,
) -> bool:
    text = "\n".join(
        (
            str(task.get("title") or ""),
            str(task.get("detail") or ""),
            contract.outcome,
            *contract.external_writes,
        )
    )
    destructive = (
        r"(?:\b(?:delete|drop|truncate|destroy|purge|wipe|remove|erase|overwrite|"
        r"reset|revoke)\b|永久(?:刪除|清除|銷毀)|刪除|清空|銷毀|移除|抹除|覆寫|重設|撤銷)"
    )
    protected = (
        r"(?:\b(?:prod(?:uction)?|database|db|remote|server|cluster|service|history)\b|"
        r"生產|正式|資料庫|遠端|伺服器|服務|叢集|歷史)"
    )
    return (
        re.search(
            rf"{destructive}.{{0,80}}{protected}|{protected}.{{0,80}}{destructive}",
            text,
            re.I | re.S,
        )
        is not None
    )


_ACCEPTANCE_COMMANDS = frozenset(
    {
        "pytest",
        "ruff",
        "rg",
        "grep",
        "tox",
        "nox",
        "echo",
        "python",
        "python3",
        "git",
        "make",
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "uv",
        "go",
        "cargo",
        "dotnet",
        "mvn",
        "gradle",
        "curl",
        "wget",
        "gh",
        "sudo",
        "env",
        "node",
        "deno",
        "tee",
        "ssh",
        "scp",
        "rsync",
        "kubectl",
        "helm",
        "aws",
        "gcloud",
        "az",
        "terraform",
        "ansible",
        "docker",
        "podman",
        "bash",
        "sh",
        "zsh",
        "dash",
        "fish",
        "rm",
        "dd",
        "mkfs",
        "shutdown",
        "reboot",
        "npx",
        "pip",
        "pip3",
        "perl",
        "ruby",
        "php",
        "pwsh",
        "powershell",
        "source",
        "eval",
        "exec",
        "xargs",
        "find",
        "sed",
        "awk",
    }
)


def _acceptance_command_body(check: str) -> tuple[str, bool]:
    label = re.match(r"^(?:command|cmd|run|執行)\s*:\s*", check, re.I)
    return (check[label.end() :] if label else check), label is not None


def _argv_stays_in_repo(argv: list[str]) -> bool:
    """拒絕 URI、絕對路徑與任何 traversal；相對路徑才可能留在 repo sandbox。"""
    for raw in argv:
        if any(ord(char) < 32 for char in raw):
            return False
        values = [raw]
        if "=" in raw:
            values.append(raw.split("=", 1)[1])
        for value in values:
            normalized = value.lstrip("@").replace("\\", "/")
            if not normalized:
                continue
            if normalized == "~" or normalized.startswith(("/", "~/")):
                return False
            if re.match(r"^[a-z][a-z0-9+.-]*:", normalized, re.I):
                return False
            if ".." in normalized.split("/"):
                return False
    return True


def _has_option(args: list[str], *names: str) -> bool:
    wanted = set(names)
    return any(arg.split("=", 1)[0] in wanted for arg in args)


def _is_safe_local_acceptance_command(check: str) -> bool:
    """只允許可明確判定為 repo 內讀取／測試／建置的 argv 形狀。"""
    line, _explicit = _acceptance_command_body(check)
    if "\r" in line or "\n" in line:
        return False
    try:
        argv = shlex.split(line)
    except ValueError:
        return False
    if not argv or not _argv_stays_in_repo(argv):
        return False
    executable = Path(argv[0]).name.casefold()
    args = argv[1:]
    if executable == "pytest":
        return True
    if executable == "ruff":
        if not args or args[0] not in {"check", "format"}:
            return False
        if _has_option(args, "--fix", "--unsafe-fixes"):
            return False
        return args[0] == "check" or "--check" in args
    if executable == "rg":
        return not _has_option(args, "--pre", "--pre-glob")
    if executable in {"grep", "echo"}:
        return True
    # tox/nox session 名稱與 repo config 都可指向 publish/deploy；V1 無法靜態證成安全。
    if executable in {"tox", "nox"}:
        return False
    if executable in {"python", "python3"}:
        return (
            len(args) >= 2
            and args[0] == "-m"
            and args[1]
            in {
                "pytest",
                "ruff",
                "compileall",
            }
        )
    if executable == "git":
        forbidden = (
            "--output",
            "--ext-diff",
            "--textconv",
            "--open-files-in-pager",
            "--exec-path",
        )
        return (
            bool(args)
            and args[0]
            in {
                "diff",
                "status",
                "log",
                "show",
                "rev-parse",
                "ls-files",
                "grep",
            }
            and not _has_option(args, *forbidden)
        )
    if executable == "make":
        targets = [arg.casefold() for arg in args if not arg.startswith("-")]
        return bool(targets) and all(
            re.fullmatch(
                r"(?:test|tests|check|lint|build|verify|ci|unit|integration|typecheck)(?:[-_:].*)?",
                target,
            )
            for target in targets
        )
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        if not args:
            return False
        if args[0] == "test":
            return True
        return (
            len(args) >= 2
            and args[0] == "run"
            and re.fullmatch(
                r"(?:test|check|lint|build|verify|typecheck)(?:[-_:].*)?",
                args[1].casefold(),
            )
        )
    if executable == "uv":
        return (
            len(args) >= 2
            and args[0] == "run"
            and _is_safe_local_acceptance_command(shlex.join(args[1:]))
        )
    if executable == "go" and _has_option(args, "-exec", "-toolexec", "-overlay"):
        return False
    if executable == "cargo" and _has_option(args, "--config"):
        return False
    allowed_subcommands = {
        "go": {"test", "vet", "build"},
        "cargo": {"test", "check", "clippy", "build", "fmt"},
        "dotnet": {"test", "build", "format"},
        "mvn": {"test", "verify", "package"},
        "gradle": {"test", "check", "build"},
    }
    return (
        executable in allowed_subcommands
        and bool(args)
        and args[0] in allowed_subcommands[executable]
    )


def _looks_like_acceptance_command(check: str) -> bool:
    body, explicit = _acceptance_command_body(check)
    try:
        argv = shlex.split(body)
    except ValueError:
        return True
    if not argv:
        return explicit
    first = argv[0]
    executable = Path(first).name.casefold()
    return bool(
        explicit
        or executable in _ACCEPTANCE_COMMANDS
        or first.startswith(("./", "../", "/", "~"))
        or "/" in first
        or "=" in first
        or (len(argv) > 1 and argv[1].startswith("-"))
    )


def _has_unsafe_acceptance(contract: TaskContract) -> bool:
    destructive = re.compile(
        r"(?<![\w./-])(?:sudo\s+)?"
        r"(?:"
        r"(?:/usr)?/bin/rm\b|rm\b|dd\b|mkfs(?:\.\w+)?\b|shutdown\b|reboot\b|"
        r"bash\s+-c\b|sh\s+-c\b|"
        r"git\b.*\b(?:push|pull|fetch|clone|ls-remote)\b|"
        r"git\s+(?:clean\s+-\S*[fdx]|reset\s+--hard\b|checkout\s+--\b|branch\s+-D\b)|"
        r"gh\b|"
        r"curl\b|wget\b|ssh\b|scp\b|rsync\b|kubectl\b|helm\b|aws\b|gcloud\b|az\b|"
        r"terraform\b|ansible\b|docker\b|podman\b|tee\b|"
        r"(?:make|npm|pnpm|yarn|bun)\b.*\b(?:publish|deploy|release|install)\b|"
        r"env\b|npx\b|pip(?:3)?\b|python3?\b.*(?:\s-c\b|\s-m\s+pip\b)|"
        r"node\b.*\s-e\b"
        r")",
        re.I,
    )
    shell_operators = re.compile(r"(?:&&|\|\||[|&;<>`$]|\r|\n)")
    for check in contract.acceptance:
        if destructive.search(check):
            return True
        if _looks_like_acceptance_command(check) and (
            shell_operators.search(check) or not _is_safe_local_acceptance_command(check)
        ):
            return True
    return False


def _acceptance_gaps(contract: TaskContract) -> tuple[str, ...]:
    """依任務類型要求可客觀覆核的證據類別，而非只接受任意一句完成宣告。"""
    checks = "\n".join(contract.acceptance).casefold()

    def has(pattern: str) -> bool:
        return re.search(pattern, checks, re.I) is not None

    if contract.kind == "implementation":
        gaps: list[str] = []
        if not has(r"(?:pytest|\btests?\b|測試|測例|spec(?:ification)?\b)"):
            gaps.append("acceptance.test")
        if not has(r"(?:\bdiff\b|\bpatch\b|artifact|產物|程式碼變更|可審查|檔案變更)"):
            gaps.append("acceptance.artifact")
        return tuple(gaps)
    if contract.kind == "investigation":
        gaps = []
        if not has(r"(?:結論|conclusion)"):
            gaps.append("acceptance.conclusion")
        if not has(r"(?:證據|evidence)"):
            gaps.append("acceptance.evidence")
        if not has(r"(?:需(?:不需要|要)?改碼|是否.{0,8}改碼|needs?.{0,4}code|code change)"):
            gaps.append("acceptance.needs_code")
        return tuple(gaps)
    if contract.kind == "docs" and not has(
        r"(?:static|lint|link|連結|內容|content|章節|spell|格式|render)"
    ):
        return ("acceptance.static_check",)
    if contract.kind == "ops":
        gaps = []
        if not has(r"(?:dry[- ]?run|試跑|演練|health|健康|readiness|smoke)"):
            gaps.append("acceptance.health_or_dry_run")
        if not has(r"(?:rollback|回滾|復原|還原)"):
            gaps.append("acceptance.rollback")
        return tuple(gaps)
    return ()


def _known_targets(context: Mapping[str, Any]) -> set[str]:
    raw = context.get("known_targets")
    if isinstance(raw, Mapping):
        known = {str(key).strip() for key, value in raw.items() if value}
    else:
        known = set(_strings(raw))

    target_evidence = context.get("target_evidence")
    if isinstance(target_evidence, Mapping):
        for target, evidence in target_evidence.items():
            exists = evidence.get("exists") if isinstance(evidence, Mapping) else evidence
            if exists is True:
                known.add(str(target).strip())

    evidence = context.get("evidence")
    if isinstance(evidence, (list, tuple)):
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind") or item.get("type") or "")
            if kind in {"target_exists", "target-known"} and item.get("exists", True) is True:
                known.add(str(item.get("target") or "").strip())
    return {target for target in known if target}


def _unresolved_targets(
    contract: TaskContract,
    context: Mapping[str, Any],
) -> tuple[str, ...]:
    known = _known_targets(context)
    explicitly_missing = set(_strings(context.get("missing_targets")))

    def resolved(target: str) -> bool:
        if target in explicitly_missing:
            return False
        return (
            "*" in known
            or target in known
            or any(item.endswith("/") and target.startswith(item) for item in known)
            or any(target.endswith("/") and item.startswith(target) for item in known)
        )

    return tuple(target for target in contract.targets if not resolved(target))


def _valid_repo_sha(value: Any) -> str:
    """只接受可唯一 pin 的完整 SHA-1/SHA-256 object id。"""
    sha = str(value or "").strip().lower()
    return sha if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) else ""


def _audit_record(
    task: Mapping[str, Any],
    context: Mapping[str, Any],
    phase: str,
    *,
    contract: TaskContract,
    outcome: str,
    reasons: tuple[str, ...],
    missing_fields: tuple[str, ...],
    unresolved_targets: tuple[str, ...],
) -> dict[str, Any]:
    canonical = json.dumps(
        contract.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    raw_task_id = task.get("id")
    task_id = (
        raw_task_id
        if isinstance(raw_task_id, int) and not isinstance(raw_task_id, bool)
        else "unknown"
    )
    repo_sha = _valid_repo_sha(context.get("repo_sha")) or "unknown"
    safe_phases = {
        "enqueue",
        "claim",
        "pre_enqueue",
        "pre_claim",
        "replay",
        "shadow",
    }
    return {
        "schema_version": 1,
        "task_id": task_id,
        "contract_version": contract.version,
        "contract_hash": hashlib.sha256(canonical).hexdigest(),
        "repo_sha": repo_sha,
        "phase": phase if phase in safe_phases else "unknown",
        "outcome": outcome,
        "rule_ids": list(reasons or (outcome,)),
        "missing_fields": list(missing_fields),
        "unresolved_target_count": len(unresolved_targets),
        # 不複製 evidence/target 內容，只保留無法還原秘密的計數。
        "evidence_summary": {"known_target_count": len(_known_targets(context))},
        "engine": "deterministic",
    }


def _decision(
    task: Mapping[str, Any],
    context: Mapping[str, Any],
    phase: str,
    *,
    contract: TaskContract,
    outcome: str,
    reasons: tuple[str, ...] = (),
    missing_fields: tuple[str, ...] = (),
    unresolved_targets: tuple[str, ...] = (),
) -> AdmissionDecision:
    return AdmissionDecision(
        outcome=outcome,
        contract=contract,
        reasons=reasons,
        missing_fields=missing_fields,
        unresolved_targets=unresolved_targets,
        audit=_audit_record(
            task,
            context,
            phase,
            contract=contract,
            outcome=outcome,
            reasons=reasons,
            missing_fields=missing_fields,
            unresolved_targets=unresolved_targets,
        ),
    )


def evaluate(
    task: Mapping[str, Any],
    context: Mapping[str, Any],
    phase: str,
) -> AdmissionDecision:
    """依呼叫端提供的事實評估任務是否可進入執行管線。"""
    contract = _contract(task)
    if _same_title_in(
        task,
        context.get("active_tasks"),
        statuses=frozenset({"pending", "in_progress", "merging"}),
    ):
        return _decision(
            task,
            context,
            phase,
            outcome="no_change",
            contract=contract,
            reasons=("duplicate_active",),
        )
    if _same_title_in(
        task,
        context.get("recent_done_tasks"),
        statuses=frozenset({"done"}),
    ):
        return _decision(
            task,
            context,
            phase,
            outcome="no_change",
            contract=contract,
            reasons=("already_done",),
        )
    if _has_unsafe_acceptance(contract):
        return _decision(
            task,
            context,
            phase,
            outcome="blocked",
            contract=contract,
            reasons=("unsafe_acceptance_command",),
        )
    if not _risk_authorized(task, context):
        return _decision(
            task,
            context,
            phase,
            outcome="blocked",
            contract=contract,
            reasons=("risk_not_authorized",),
        )
    if _has_irreversible_intent(task, contract) and not (
        task.get("human_approved") is True or context.get("human_approved") is True
    ):
        return _decision(
            task,
            context,
            phase,
            outcome="blocked",
            contract=contract,
            reasons=("irreversible_intent_not_approved",),
        )
    if _has_external_mutation_intent(task, contract) and not contract.external_writes:
        return _decision(
            task,
            context,
            phase,
            outcome="blocked",
            contract=contract,
            reasons=("external_write_not_declared",),
        )
    if not _external_writes_authorized(contract, context):
        return _decision(
            task,
            context,
            phase,
            outcome="blocked",
            contract=contract,
            reasons=("external_write_not_authorized",),
        )
    missing = tuple(
        field_name
        for field_name, value in (
            ("outcome", contract.outcome),
            ("kind", contract.kind),
            ("targets", contract.targets),
            ("acceptance", contract.acceptance),
        )
        if not value
    )
    if missing:
        return _decision(
            task,
            context,
            phase,
            outcome="needs_clarification",
            contract=contract,
            reasons=("contract_fields_missing",),
            missing_fields=missing,
        )
    schema_gaps: list[str] = []
    if contract.version != CONTRACT_VERSION:
        schema_gaps.append("version")
    if contract.kind not in CONTRACT_KINDS:
        schema_gaps.append("kind")
    if schema_gaps:
        return _decision(
            task,
            context,
            phase,
            outcome="needs_clarification",
            contract=contract,
            reasons=("contract_schema_invalid",),
            missing_fields=tuple(schema_gaps),
        )
    unresolved = _unresolved_targets(contract, context)
    if unresolved:
        if (contract.kind == "ops" or contract.external_writes) and any(
            _is_external_target(target) for target in unresolved
        ):
            return _decision(
                task,
                context,
                phase,
                outcome="blocked",
                contract=contract,
                reasons=("external_target_evidence_missing",),
                unresolved_targets=unresolved,
            )
        return _decision(
            task,
            context,
            phase,
            outcome="needs_clarification",
            contract=contract,
            reasons=("target_not_found",),
            unresolved_targets=unresolved,
        )
    acceptance_gaps = _acceptance_gaps(contract)
    if acceptance_gaps:
        return _decision(
            task,
            context,
            phase,
            outcome="needs_clarification",
            contract=contract,
            reasons=("acceptance_evidence_missing",),
            missing_fields=acceptance_gaps,
        )
    if contract.kind == "investigation":
        return _decision(
            task,
            context,
            phase,
            outcome="investigation",
            contract=contract,
            reasons=("investigation_lane",),
        )
    return _decision(task, context, phase, outcome="ready", contract=contract)


def build_local_context(
    task: Mapping[str, Any],
    repo_context: Mapping[str, Any],
    *,
    tasks: Any = (),
) -> dict[str, Any]:
    """以 repo 內唯讀檔案證據建 context；外部 target 只能由明確 adapter 證據證成。

    ``root`` 與 ``repo_sha`` 都由呼叫端注入，所以此 adapter 不綁 Ti 的全域設定，也
    不做網路或 GitHub 呼叫。回傳只含 target 名稱與 task 最小欄位，不複製檔案內容。
    """
    try:
        root = Path(repo_context.get("root") or ".").resolve()
    except (OSError, TypeError, ValueError):
        root = Path(".").resolve()
    contract = _contract(task)
    known = set(_strings(repo_context.get("known_targets")))
    explicitly_missing = set(_strings(repo_context.get("missing_targets")))

    for target in contract.targets:
        if target in known or target in explicitly_missing:
            continue
        # URI/adapter identity 不可用本機 Path.exists 猜成事實，必須由 injected evidence 提供。
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or "\x00" in target:
            continue
        candidate = Path(target)
        if candidate.is_absolute():
            explicitly_missing.add(target)
            continue
        try:
            resolved = (root / candidate).resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            explicitly_missing.add(target)
            continue
        if resolved.exists():
            known.add(target)
        else:
            explicitly_missing.add(target)

    active: list[dict[str, Any]] = []
    recent_done: list[dict[str, Any]] = []
    if isinstance(tasks, (list, tuple)):
        done_rows: list[Mapping[str, Any]] = []
        for row in tasks:
            if not isinstance(row, Mapping):
                continue
            status = str(row.get("status") or "")
            minimal = {
                "id": row.get("id"),
                "title": str(row.get("title") or "")[:500],
                "status": status,
            }
            if status in {"pending", "in_progress", "merging"}:
                active.append(minimal)
            elif status == "done":
                done_rows.append(row)
        done_rows.sort(
            key=lambda row: float(row.get("updated_at") or 0),
            reverse=True,
        )
        recent_done = [
            {
                "id": row.get("id"),
                "title": str(row.get("title") or "")[:500],
                "status": "done",
            }
            for row in done_rows[:50]
        ]

    context: dict[str, Any] = {
        "known_targets": sorted(known),
        "missing_targets": sorted(explicitly_missing),
        "active_tasks": active,
        "recent_done_tasks": recent_done,
        "repo_sha": str(repo_context.get("repo_sha") or ""),
    }
    # 只有明確列出的能力/授權欄位可穿透；raw prompt、env、輸出與檔案內容皆不會進 context。
    for key in (
        "target_evidence",
        "evidence",
        "authorized_external_writes",
        "external_write_allowed",
        "authorized_risks",
        "risk_authorized",
        "human_approved",
    ):
        if key in repo_context:
            context[key] = repo_context[key]
    return context


def read_local_repo_sha(root: Path | str) -> str:
    """不啟動 git/網路程序，從本機 .git metadata 讀 HEAD；無法證實則回 unknown。"""
    try:
        repo_root = Path(root).resolve()
        git_dir = repo_root / ".git"
        if git_dir.is_file():
            marker = git_dir.read_text(encoding="utf-8").strip()
            if not marker.startswith("gitdir:"):
                return "unknown"
            candidate = Path(marker.split(":", 1)[1].strip())
            git_dir = (
                (repo_root / candidate).resolve() if not candidate.is_absolute() else candidate
            )
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-fA-F]{7,64}", head):
            return head.lower()
        if not head.startswith("ref:"):
            return "unknown"
        ref = head.split(":", 1)[1].strip()
        ref_roots = [git_dir]
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            common_value = commondir_file.read_text(encoding="utf-8").strip()
            common_dir = Path(common_value)
            if not common_dir.is_absolute():
                common_dir = (git_dir / common_dir).resolve()
            if common_dir not in ref_roots:
                ref_roots.append(common_dir)
        for ref_root in ref_roots:
            ref_path = ref_root / ref
            if ref_path.is_file():
                value = ref_path.read_text(encoding="utf-8").strip()
                return value.lower() if re.fullmatch(r"[0-9a-fA-F]{7,64}", value) else "unknown"
            packed = ref_root / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if not line or line.startswith(("#", "^")):
                        continue
                    value, _, name = line.partition(" ")
                    if name == ref and re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
                        return value.lower()
    except (OSError, TypeError, ValueError):
        return "unknown"
    return "unknown"


_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{12,}|sk-[A-Za-z0-9_-]{12,}|"
    r"(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+)"
)
_URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@")


def _safe_model_text(value: Any, limit: int = 2000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    text = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
    return _SECRET_VALUE_RE.sub("[REDACTED]", text)[:limit]


def _safe_contract_for_model(contract: TaskContract) -> dict[str, Any]:
    """只投影 resolver 必需欄位，且對每個自由文字值逐一去敏。"""
    raw = contract.to_dict()
    return {
        "version": raw["version"],
        "outcome": _safe_model_text(raw["outcome"]),
        "kind": _safe_model_text(raw["kind"], 50),
        "targets": [_safe_model_text(item, 500) for item in raw["targets"][:50]],
        "acceptance": [_safe_model_text(item, 500) for item in raw["acceptance"][:50]],
        "constraints": [_safe_model_text(item, 500) for item in raw["constraints"][:50]],
        "external_writes": [_safe_model_text(item, 500) for item in raw["external_writes"][:50]],
    }


def _model_contract(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    # 白名單欄位；模型輸出的 risk、授權、evidence、ready 宣告一律不會穿透。
    raw = {
        key: value.get(key)
        for key in (
            "version",
            "outcome",
            "kind",
            "targets",
            "acceptance",
            "constraints",
            "external_writes",
        )
        if key in value
    }
    contract = _contract({"contract": raw})
    result = contract.to_dict()
    result["outcome"] = _safe_model_text(result["outcome"])
    for key in ("targets", "acceptance", "constraints", "external_writes"):
        result[key] = [_safe_model_text(item, 500) for item in result[key]][:50]
    return result


def _semantic_cache_path(
    cache_dir: Path,
    *,
    contract_hash: str,
    repo_sha: str,
    semantic_hash: str,
) -> Path:
    key = hashlib.sha256(f"{contract_hash}:{repo_sha}:{semantic_hash}".encode()).hexdigest()
    return cache_dir / f"{key}.json"


def _semantic_task_hash(task: Mapping[str, Any]) -> str:
    """區分會影響 resolver 提案的 task 語意；只落不可逆 digest。"""
    payload = {
        "task_id": task.get("id"),
        "title": str(task.get("title") or ""),
        "detail": str(task.get("detail") or ""),
        "risk": str(task.get("risk") or ""),
        "source": str(task.get("source") or ""),
        "type": str(task.get("type") or ""),
        "eligible": task.get("eligible"),
        "lane": str(task.get("lane") or ""),
        "human_approved": task.get("human_approved") is True,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _semantic_cache_lock(path: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), str(path))
    lock = _SEMANTIC_CACHE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SEMANTIC_CACHE_LOCKS[key] = lock
    return lock


@contextlib.asynccontextmanager
async def _semantic_cache_file_lock(path: Path, *, timeout_s: float):
    """跨行程序列化同一 scope 的 resolver；等待採非阻塞 flock，不卡住 event loop。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = (path.parent / f"{path.name}.lock").open("a+", encoding="utf-8")
    acquired = False
    deadline = time.monotonic() + max(1.0, float(timeout_s)) + 5.0
    try:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("semantic cache lock timeout") from None
                await asyncio.sleep(0.01)
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _write_semantic_cache(path: Path, value: Mapping[str, Any]) -> None:
    """安全原子寫入不含 prompt/output 的 resolver scope 狀態。"""
    from .secure_write import secure_write_root

    path.parent.mkdir(parents=True, exist_ok=True)
    secure_write_root(
        path,
        (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )


async def evaluate_with_semantic_fallback(
    task: Mapping[str, Any],
    repo_context: Mapping[str, Any],
    phase: str,
    *,
    resolver: Any,
    cache_dir: Path,
    tasks: Any = (),
    timeout_s: float = 30.0,
) -> tuple[AdmissionDecision, dict[str, Any]]:
    """規則先行；僅語意不完整時至多呼叫一次 resolver，再交確定性規則重驗。

    resolver 只能提議 contract，不能提供事實證據、降風險、授權 external write 或
    直接宣告 ready。cache scope 至少綁原 contract hash + repo SHA，另納入穩定 task
    語意，避免不同任務共用不相干的補全提案。
    """
    context = build_local_context(task, repo_context, tasks=tasks)
    initial = evaluate(task, context, phase)
    meta: dict[str, Any] = {
        "cache_hit": False,
        "model_calls": 0,
        "model": None,
        "token_usage": None,
        "error": "",
    }
    if (
        initial.outcome != "needs_clarification"
        or not set(initial.reasons).intersection(_QUALITY_RULES)
        or resolver is None
    ):
        return initial, meta

    repo_sha = str(initial.audit.get("repo_sha") or "unknown")
    if repo_sha == "unknown":
        meta["error"] = "repo_sha_unknown"
        return initial, meta
    cache_path = _semantic_cache_path(
        cache_dir,
        contract_hash=str(initial.audit.get("contract_hash") or ""),
        repo_sha=repo_sha,
        semantic_hash=_semantic_task_hash(task),
    )
    cache_key = str(cache_path)

    def finalize_response(
        response: Mapping[str, Any],
        *,
        cache_hit: bool,
    ) -> dict[str, Any] | None:
        """驗證模型提案並在 fresh path durable terminal state；caller 必須持 file lock。"""
        proposal = _model_contract(response.get("contract"))
        if proposal is None:
            meta["error"] = "invalid_contract"
            if not cache_hit:
                try:
                    _write_semantic_cache(
                        cache_path,
                        {
                            "version": 1,
                            "status": "error",
                            "error": meta["error"],
                            "retry_after": time.time() + 60.0,
                        },
                    )
                except Exception:  # noqa: BLE001 — 不得把無法 durable 的提案視為已處理
                    meta["error"] = "cache_write_failed"
            return None
        meta["model"] = _safe_model_text(response.get("model"), 100) or None
        usage = response.get("token_usage")
        if isinstance(usage, Mapping):
            meta["token_usage"] = {
                key: int(value)
                for key in ("input", "output", "total")
                if isinstance((value := usage.get(key)), int) and not isinstance(value, bool)
            }
        if not cache_hit:
            cached_value = {
                "version": 1,
                "status": "success",
                "contract": proposal,
                "model": meta["model"],
                "token_usage": meta["token_usage"],
            }
            try:
                _write_semantic_cache(cache_path, cached_value)
            except Exception:  # noqa: BLE001 — 無 durable cache 就不得釋放模型提案
                meta["error"] = "cache_write_failed"
                return None
            if len(_SEMANTIC_MEMORY_CACHE) >= 1024:
                _SEMANTIC_MEMORY_CACHE.pop(next(iter(_SEMANTIC_MEMORY_CACHE)))
            _SEMANTIC_MEMORY_CACHE[cache_key] = cached_value
        return proposal

    async with _semantic_cache_lock(cache_path):
        cached = _SEMANTIC_MEMORY_CACHE.get(cache_key)
        response: Mapping[str, Any] | None = (
            cached if cached is not None and isinstance(cached.get("contract"), Mapping) else None
        )
        proposal: dict[str, Any] | None = None
        if response is not None:
            meta["cache_hit"] = True
            proposal = finalize_response(response, cache_hit=True)
            if proposal is None:
                return initial, meta
        else:
            try:
                async with _semantic_cache_file_lock(cache_path, timeout_s=timeout_s):
                    # 另一 coroutine 可能在本 coroutine 等跨行程鎖時已完成並填入 memory。
                    cached = _SEMANTIC_MEMORY_CACHE.get(cache_key)
                    if cached is None:
                        try:
                            raw_cached = json.loads(cache_path.read_text(encoding="utf-8"))
                        except FileNotFoundError:
                            raw_cached = None
                        except (OSError, ValueError):
                            meta["error"] = "cache_read_failed"
                            return initial, meta
                        if raw_cached is not None and not isinstance(raw_cached, dict):
                            meta["error"] = "cache_read_failed"
                            return initial, meta
                        cached = raw_cached

                    if cached is not None and isinstance(cached.get("contract"), Mapping):
                        response = cached
                        meta["cache_hit"] = True
                    elif cached is not None and cached.get("status") in {
                        "in_progress",
                        "error",
                    }:
                        try:
                            retry_after = float(cached.get("retry_after") or 0)
                        except (TypeError, ValueError):
                            meta["error"] = "cache_read_failed"
                            return initial, meta
                        if retry_after > time.time():
                            meta["cache_hit"] = True
                            meta["error"] = (
                                _safe_model_text(cached.get("error"), 100)
                                if cached.get("status") == "error"
                                else "semantic_in_progress"
                            ) or "resolver_error"
                            return initial, meta
                        cached = None
                    elif cached is not None:
                        meta["error"] = "cache_read_failed"
                        return initial, meta

                    if response is None:
                        # 先 durable 宣告本 scope 已開始，寫不下就絕不花模型成本或放行。
                        try:
                            _write_semantic_cache(
                                cache_path,
                                {
                                    "version": 1,
                                    "status": "in_progress",
                                    "retry_after": time.time() + max(1.0, float(timeout_s)) + 5.0,
                                },
                            )
                        except Exception:  # noqa: BLE001 — cache 是 refine-once 的安全邊界
                            meta["error"] = "cache_write_failed"
                            return initial, meta

                        payload = {
                            "task_id": task.get("id"),
                            "title": _safe_model_text(task.get("title"), 500),
                            "detail": _safe_model_text(task.get("detail"), 4000),
                            "risk": _safe_model_text(task.get("risk"), 30),
                            "repo_sha": repo_sha,
                            "current_contract": _safe_contract_for_model(initial.contract),
                            "missing_fields": list(initial.missing_fields),
                            "unresolved_target_count": len(initial.unresolved_targets),
                        }
                        meta["model_calls"] = 1
                        try:
                            resolved = await asyncio.wait_for(
                                resolver(payload),
                                timeout=max(0.001, timeout_s),
                            )
                        except TimeoutError:
                            meta["error"] = "timeout"
                            resolved = None
                        except Exception:  # noqa: BLE001 — resolver 失敗必須 fail-closed
                            meta["error"] = "resolver_error"
                            resolved = None

                        if not meta["error"] and not isinstance(resolved, Mapping):
                            meta["error"] = "invalid_response"
                        if meta["error"]:
                            try:
                                _write_semantic_cache(
                                    cache_path,
                                    {
                                        "version": 1,
                                        "status": "error",
                                        "error": meta["error"],
                                        # 內部錯誤依既定政策可重試，但同一冷卻窗不重打。
                                        "retry_after": time.time() + 60.0,
                                    },
                                )
                            except Exception:  # noqa: BLE001 — 原 in_progress sentinel 仍 fail-closed
                                meta["error"] = "cache_write_failed"
                            return initial, meta
                        response = resolved
                    assert response is not None
                    # terminal success/error 必須在 file lock 內落盤，waiter 才不會把短暫
                    # in_progress 誤當本 task 的最終 internal error。
                    proposal = finalize_response(response, cache_hit=bool(meta["cache_hit"]))
                    if proposal is None:
                        return initial, meta
            except TimeoutError:
                meta["error"] = "cache_lock_timeout"
                return initial, meta
            except Exception:  # noqa: BLE001 — lock/open 失敗時不可繞過 refine-once
                meta["error"] = "cache_lock_error"
                return initial, meta
        assert proposal is not None

    enriched = {**task, "contract": proposal}
    enriched_context = build_local_context(enriched, repo_context, tasks=tasks)
    return evaluate(enriched, enriched_context, phase), meta


def decision_record(
    decision: AdmissionDecision,
    task: Mapping[str, Any],
    *,
    mode: str,
    phase: str,
    evaluated_at: float | None = None,
) -> dict[str, Any]:
    """把裁決縮成可持久化/UI 投影的去敏資料，並建立 task+contract+SHA scope。"""
    source = str(task.get("source") or "").strip().lower()
    human_source = source in _HUMAN_SOURCES
    needs_attention = decision.outcome in {"needs_clarification", "blocked"}
    needs_human = human_source and needs_attention
    quality_only = bool(decision.reasons) and set(decision.reasons).issubset(_QUALITY_RULES)
    overridable = decision.outcome == "needs_clarification" and quality_only
    missing = [str(item)[:100] for item in decision.missing_fields[:20]]
    reasons = [str(item)[:100] for item in decision.reasons[:20]]
    if needs_human and decision.outcome == "needs_clarification":
        fields = "、".join(missing) if missing else "目標或證據"
        question = f"請確認此任務缺少的 {fields}；是否採用建議後再執行？"
        recommendation = f"先補齊 {fields}，再重新進行准入評估。"
    elif needs_human:
        question = "此任務被安全規則阻擋；是否調整範圍或補上既有治理要求的授權？"
        recommendation = "不要繞過治理閘；縮小風險或走既有核准流程。"
    else:
        question = ""
        recommendation = ""

    contract_hash = str(decision.audit.get("contract_hash") or "")
    repo_sha = str(decision.audit.get("repo_sha") or "unknown")
    scope_payload = json.dumps(
        {
            "task_id": task.get("id"),
            "task_hash": _semantic_task_hash(task),
            "contract_hash": contract_hash,
            "repo_sha": repo_sha,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "version": 1,
        "outcome": decision.outcome,
        "reasons": reasons,
        "missing_fields": missing,
        "unresolved_target_count": len(decision.unresolved_targets),
        "needs_human": needs_human,
        "overridable": overridable,
        "question": question,
        "recommendation": recommendation,
        "timeout_default": (
            "investigation"
            if needs_human
            and decision.outcome == "needs_clarification"
            and str(task.get("risk") or "").lower() == "low"
            else "park"
        ),
        "scope_hash": hashlib.sha256(scope_payload).hexdigest(),
        "mode": mode if mode in {"off", "shadow", "enforce"} else "shadow",
        "phase": phase if phase in {"enqueue", "claim", "replay", "shadow"} else "unknown",
        "evaluated_at": time.time() if evaluated_at is None else float(evaluated_at),
        "engine": "deterministic",
        "cache_hit": False,
        "model_calls": 0,
        "model": None,
        "token_usage": None,
        "model_error": "",
        "audit": dict(decision.audit),
    }


def _append_decision_audit(
    record: Mapping[str, Any],
    *,
    state_dir: Path | None,
    latency_ms: float,
    human_review: bool = False,
    override_reason: str = "",
) -> None:
    """只 append 去敏裁決資料；觀測寫入失敗由 jsonl_log 吞掉，不改控制流。"""
    from . import config, jsonl_log

    audit = dict(record.get("audit") or {})
    audit.update(
        {
            "mode": record.get("mode"),
            "phase": record.get("phase"),
            "scope_hash": record.get("scope_hash"),
            "needs_human": bool(record.get("needs_human")),
            "overridable": bool(record.get("overridable")),
            "latency_ms": round(max(0.0, latency_ms), 3),
            "cache_hit": bool(record.get("cache_hit")),
            "model_calls": int(record.get("model_calls") or 0),
            "model": record.get("model"),
            "token_usage": record.get("token_usage"),
            "model_error": record.get("model_error") or "",
            "human_review": human_review or bool(record.get("human_review")),
        }
    )
    if override_reason:
        audit["override_reason_hash"] = hashlib.sha256(override_reason.encode()).hexdigest()
    root = state_dir if state_dir is not None else config.AUTOPILOT_STATE_DIR
    jsonl_log.append(root / "admission_audit.jsonl", audit)


def _internal_error_record(
    task: Mapping[str, Any],
    repo_context: Mapping[str, Any],
    *,
    mode: str,
    phase: str,
) -> tuple[AdmissionDecision, dict[str, Any]]:
    """建立不含例外訊息的 fail-closed 紀錄；不可把 prompt、輸出或秘密寫進 task/audit。"""
    contract = _contract(task)
    context = {"repo_sha": str(repo_context.get("repo_sha") or "")}
    decision = _decision(
        task,
        context,
        phase,
        contract=contract,
        outcome="blocked",
        reasons=("admission_internal_error",),
    )
    record = decision_record(decision, task, mode=mode, phase=phase)
    record.update(
        {
            "needs_human": False,
            "overridable": False,
            "question": "",
            "recommendation": "",
            "timeout_default": "park",
            "model_error": "internal_error",
        }
    )
    return decision, record


def _defer_internal_error(
    task: Mapping[str, Any],
    expected_fingerprint: str,
    repo_context: Mapping[str, Any],
    *,
    mode: str,
    phase: str,
    state_dir: Path | None,
    started: float,
    mode_generation: int | None = None,
) -> tuple[bool, bool]:
    """內部例外不放行、不增 attempts；回傳 (已持久化, circuit paused)。"""
    from . import backlog

    persisted = False
    try:
        decision, record = _internal_error_record(
            task,
            repo_context,
            mode=mode,
            phase=phase,
        )
        committed, _error = backlog.commit_admission(
            int(task["id"]),
            expected_fingerprint,
            contract=decision.contract.to_dict(),
            admission=record,
            transition="record",
            retry_after=time.time() + 60.0,
            state_dir=state_dir,
            expected_mode=mode,
            expected_mode_generation=mode_generation,
        )
        if _error == "mode_changed":
            return False, False
        persisted = committed is not None
        if persisted:
            _append_decision_audit(
                record,
                state_dir=state_dir,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
    except Exception:  # noqa: BLE001 — 二次寫入也失敗時仍須熔斷，原 task 保持原狀
        persisted = False
    circuit = _record_admission_circuit("internal_error", state_dir=state_dir)
    return persisted, bool(circuit.get("paused"))


def _requeue_claim_after_circuit_failure(
    committed: Any,
    repo_context: Mapping[str, Any],
    *,
    mode: str,
    phase: str,
    state_dir: Path | None,
    started: float,
) -> bool:
    """成功 CAS claim 後若 circuit reset 失敗，原子退還 claim，絕不交給 runner。"""
    from . import backlog

    try:
        decision, record = _internal_error_record(
            committed.task,
            repo_context,
            mode=mode,
            phase=phase,
        )
        updated, _error = backlog.requeue_admission_claim(
            int(committed.task["id"]),
            backlog.task_fingerprint(committed.task),
            contract=decision.contract.to_dict(),
            admission=record,
            attempts_before=committed.attempts_before,
            retry_after=time.time() + 60.0,
            state_dir=state_dir,
        )
        if updated is None:
            return False
        _append_decision_audit(
            record,
            state_dir=state_dir,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return True
    except Exception:  # noqa: BLE001 — circuit 已 paused；回滾失敗交 stale reaper
        return False


def defer_claimed_execution_error(
    task: Mapping[str, Any],
    repo_context: Mapping[str, Any],
    *,
    state_dir: Path | None = None,
) -> bool:
    """claim 已提交、runner 尚未開始時遇到版本/adapter 錯誤：CAS 退還且不耗 attempt。"""
    from . import backlog

    started = time.perf_counter()
    persisted = False
    try:
        current = backlog.get(int(task["id"]), state_dir=state_dir)
        if current is None or current.get("status") != "in_progress":
            raise RuntimeError("claim no longer owned")
        decision, record = _internal_error_record(
            current,
            repo_context,
            mode="enforce",
            phase="claim",
        )
        updated, _error = backlog.requeue_admission_claim(
            int(current["id"]),
            backlog.task_fingerprint(current),
            contract=decision.contract.to_dict(),
            admission=record,
            attempts_before=max(0, int(task.get("attempts") or 0)),
            retry_after=time.time() + 60.0,
            state_dir=state_dir,
        )
        persisted = updated is not None
        if persisted:
            _append_decision_audit(
                record,
                state_dir=state_dir,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
    except Exception:  # noqa: BLE001 — task 仍不可交給 runner，stale reaper 為最後兜底
        persisted = False
    _record_admission_circuit("internal_error", state_dir=state_dir)
    return persisted


def _shadow_claim_after_internal_error(
    task: Mapping[str, Any],
    expected_fingerprint: str,
    repo_context: Mapping[str, Any],
    *,
    state_dir: Path | None,
    started: float,
    mode_generation: int | None = None,
) -> AdmissionSelection | None:
    """shadow 的觀測器壞掉仍原子沿用舊派工，只把安全錯誤標籤留痕。"""
    from . import backlog

    try:
        decision, record = _internal_error_record(
            task,
            repo_context,
            mode="shadow",
            phase="claim",
        )
        committed, _error = backlog.commit_admission(
            int(task["id"]),
            expected_fingerprint,
            contract=decision.contract.to_dict(),
            admission=record,
            transition="claim",
            state_dir=state_dir,
            expected_mode="shadow",
            expected_mode_generation=mode_generation,
        )
        if committed is None:
            return None
        _append_decision_audit(
            record,
            state_dir=state_dir,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        execution_task = dict(committed.task)
        execution_task["attempts"] = committed.attempts_before
        return AdmissionSelection(task=execution_task, decision=decision)
    except Exception:  # noqa: BLE001 — shadow 不可因觀測器錯誤炸掉 worker
        try:
            decision, _record = _internal_error_record(
                task,
                repo_context,
                mode="shadow",
                phase="claim",
            )
            return _shadow_legacy_claim(
                task,
                expected_fingerprint,
                decision,
                state_dir=state_dir,
                mode_generation=mode_generation,
            )
        except Exception:  # noqa: BLE001 — storage 本身不可用時只能等下輪
            return None


def _shadow_legacy_claim(
    task: Mapping[str, Any],
    expected_fingerprint: str,
    decision: AdmissionDecision,
    *,
    state_dir: Path | None,
    mode_generation: int | None = None,
) -> AdmissionSelection | None:
    """shadow 觀測落檔故障時只做舊語意 CAS claim，仍避免主／旁路重複執行。"""
    from . import backlog

    committed, _error = backlog.claim_if_unchanged(
        int(task["id"]),
        expected_fingerprint,
        state_dir=state_dir,
        expected_mode="shadow",
        expected_mode_generation=mode_generation,
    )
    if committed is None:
        return None
    execution_task = dict(committed.task)
    execution_task["attempts"] = committed.attempts_before
    return AdmissionSelection(task=execution_task, decision=decision)


def apply_override(
    task_id: int,
    scope_hash: str,
    reason: str,
    *,
    repo_context: Mapping[str, Any],
    state_dir: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """以目前 contract+repo SHA 重算 scope 後，原子套用一次性 quality override。"""
    from . import backlog

    clean_reason = _safe_model_text(reason, 500)
    if not clean_reason:
        return None, "invalid_reason"
    snapshot = backlog.get(task_id, state_dir=state_dir)
    if snapshot is None:
        return None, "not_found"
    prior_admission = snapshot.get("admission")
    if isinstance(prior_admission, dict) and isinstance(prior_admission.get("override"), dict):
        return None, "already_overridden"
    context = build_local_context(
        snapshot,
        repo_context,
        tasks=backlog.list_tasks(state_dir=state_dir),
    )
    decision = evaluate(snapshot, context, "claim")
    record = decision_record(decision, snapshot, mode="enforce", phase="claim")
    if not _valid_repo_sha(record["audit"].get("repo_sha")):
        return None, "unknown_repo_sha"
    persisted_scope = (
        str(prior_admission.get("scope_hash") or "") if isinstance(prior_admission, dict) else ""
    )
    if scope_hash != persisted_scope:
        return None, "stale_scope"
    if scope_hash != record["scope_hash"]:
        if decision.outcome in {"ready", "investigation"}:
            transition = "release"
        elif decision.outcome == "no_change":
            transition = "complete"
        else:
            transition = "record"
        refreshed, _refresh_error = backlog.commit_parked_admission(
            task_id,
            backlog.task_fingerprint(snapshot),
            contract=decision.contract.to_dict(),
            admission=record,
            transition=transition,
            attempts=int(snapshot.get("attempts") or 0),
            state_dir=state_dir,
        )
        if refreshed is None:
            return None, "stale_scope"
        _append_decision_audit(
            record,
            state_dir=state_dir,
            latency_ms=0.0,
            human_review=True,
        )
        return None, "stale_scope_refreshed"
    if record["overridable"] is not True or not (
        isinstance(prior_admission, dict) and prior_admission.get("overridable") is True
    ):
        return None, "not_overridable"
    updated, error = backlog.apply_admission_override(
        task_id,
        backlog.task_fingerprint(snapshot),
        scope_hash,
        clean_reason,
        state_dir=state_dir,
    )
    if updated is None:
        return None, error
    audit_record = {
        **record,
        "outcome": "ready",
        "audit": {
            **record["audit"],
            "outcome": "ready",
            "rule_ids": ["admin_quality_override"],
        },
    }
    _append_decision_audit(
        audit_record,
        state_dir=state_dir,
        latency_ms=0.0,
        human_review=True,
        override_reason=clean_reason,
    )
    return updated, ""


def enqueue_task(
    title: str,
    detail: str = "",
    source: str = "seed",
    *,
    mode: str,
    repo_context: Mapping[str, Any],
    state_dir: Path | None = None,
    mode_generation: int | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    """建立 task 後立即做 ingest admission；off 完整保留舊 add 行為。"""
    if mode not in {"off", "shadow", "enforce"}:
        raise ValueError("enqueue_task mode must be off, shadow, or enforce")
    from . import backlog

    if mode == "off":
        # kill switch 必須能完整退回舊 task shape；新 schema 欄位不穿透。
        fields.pop("contract", None)
        fields.pop("admission", None)
    task = backlog.add(
        title,
        detail,
        source=source,
        state_dir=state_dir,
        **fields,
    )
    if task is None or mode == "off":
        return task
    started = time.perf_counter()
    try:
        fingerprint = backlog.task_fingerprint(task)
    except Exception:  # noqa: BLE001 — 已建立 task 仍留 pending；enforce 另熔斷
        if mode == "enforce":
            _record_admission_circuit("internal_error", state_dir=state_dir)
        return task
    if mode == "enforce" and not _valid_repo_sha(repo_context.get("repo_sha")):
        _defer_internal_error(
            task,
            fingerprint,
            repo_context,
            mode=mode,
            phase="enqueue",
            state_dir=state_dir,
            started=started,
            mode_generation=mode_generation,
        )
        return backlog.get(task["id"], state_dir=state_dir) or task
    try:
        context = build_local_context(
            task,
            repo_context,
            tasks=backlog.list_tasks(state_dir=state_dir),
        )
        decision = evaluate(task, context, "enqueue")
        record = decision_record(decision, task, mode=mode, phase="enqueue")
        if mode == "enforce":
            circuit = admission_circuit_state(state_dir=state_dir)
            if circuit.get("paused"):
                _defer_internal_error(
                    task,
                    fingerprint,
                    repo_context,
                    mode=mode,
                    phase="enqueue",
                    state_dir=state_dir,
                    started=started,
                    mode_generation=mode_generation,
                )
                return backlog.get(task["id"], state_dir=state_dir) or task
        automated_clarification = (
            decision.outcome == "needs_clarification"
            and str(source or "").strip().lower() not in _HUMAN_SOURCES
        )
        if (
            mode == "shadow"
            or decision.outcome in {"ready", "investigation"}
            or automated_clarification
        ):
            transition = "record"
        elif decision.outcome == "no_change":
            transition = "complete"
        else:
            transition = "park"
        committed, _error = backlog.commit_admission(
            task["id"],
            fingerprint,
            contract=decision.contract.to_dict(),
            admission=record,
            transition=transition,
            state_dir=state_dir,
            expected_mode=mode,
            expected_mode_generation=mode_generation,
        )
    except Exception:  # noqa: BLE001 — ingest observer/gate 例外不得讓 caller 誤以為 add 失敗
        if mode == "enforce":
            _defer_internal_error(
                task,
                fingerprint,
                repo_context,
                mode=mode,
                phase="enqueue",
                state_dir=state_dir,
                started=started,
                mode_generation=mode_generation,
            )
        else:
            try:
                decision, record = _internal_error_record(
                    task,
                    repo_context,
                    mode=mode,
                    phase="enqueue",
                )
                committed, _error = backlog.commit_admission(
                    task["id"],
                    fingerprint,
                    contract=decision.contract.to_dict(),
                    admission=record,
                    transition="record",
                    state_dir=state_dir,
                    expected_mode=mode,
                    expected_mode_generation=mode_generation,
                )
                if committed is not None:
                    _append_decision_audit(
                        record,
                        state_dir=state_dir,
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )
            except Exception:  # noqa: BLE001 — shadow 觀測失敗完全不改舊 add 成果
                pass
        return backlog.get(task["id"], state_dir=state_dir) or task
    if committed is not None:
        if mode == "enforce":
            _record_admission_circuit(None, state_dir=state_dir)
        _append_decision_audit(
            record,
            state_dir=state_dir,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return committed.task
    return backlog.get(task["id"], state_dir=state_dir) or task


def enqueue_many(
    titles: list[str],
    source: str = "discovered",
    *,
    mode: str,
    repo_context: Mapping[str, Any],
    state_dir: Path | None = None,
    gen: int = 0,
    mode_generation: int | None = None,
) -> int:
    """批次 ingest 純標題，回傳實際建立數（含 enforce 後 parked/done 的可稽核項目）。"""
    return sum(
        enqueue_task(
            title,
            source=source,
            mode=mode,
            repo_context=repo_context,
            state_dir=state_dir,
            gen=gen,
            mode_generation=mode_generation,
        )
        is not None
        for title in titles
    )


def enqueue_items(
    items: list[dict],
    source: str = "discovered",
    *,
    mode: str,
    repo_context: Mapping[str, Any],
    state_dir: Path | None = None,
    gen: int = 0,
    mode_generation: int | None = None,
) -> int:
    """批次 ingest 結構化任務；生成內容永遠不能自設 human_approved。"""
    from . import backlog

    count = 0
    for item in items:
        task = enqueue_task(
            item.get("title", ""),
            item.get("detail", ""),
            source=source,
            mode=mode,
            repo_context=repo_context,
            state_dir=state_dir,
            mode_generation=mode_generation,
            priority=item.get("priority", backlog.DEFAULT_PRIORITY),
            item_type=item.get("type", "improvement"),
            effort=item.get("effort", ""),
            gen=gen,
            risk=item.get("risk", "medium"),
            eligible=item.get("eligible", True),
            exclusion_reason=item.get("exclusion_reason", ""),
            rollback=item.get("rollback"),
            approval_verdicts=item.get("approval_verdicts"),
            diff_sha=item.get("diff_sha", ""),
            evidence_sha=item.get("evidence_sha", ""),
            human_approved=False,
            contract=item.get("contract") if isinstance(item.get("contract"), dict) else None,
            split_depth=item.get("split_depth"),
        )
        if task is not None:
            count += 1
    return count


def _consume_valid_override(
    snapshot: Mapping[str, Any],
    decision: AdmissionDecision,
    record: dict[str, Any],
) -> bool:
    admission = snapshot.get("admission")
    override = admission.get("override") if isinstance(admission, Mapping) else None
    if not isinstance(override, Mapping) or override.get("consumed_at"):
        return False
    if record.get("overridable") is not True:
        return False
    if str(override.get("scope_hash") or "") != str(record.get("scope_hash") or ""):
        return False
    consumed = {
        "scope_hash": str(override.get("scope_hash") or ""),
        "reason": _safe_model_text(override.get("reason"), 500),
        "actor": "admin",
        "applied_at": override.get("applied_at"),
        "consumed_at": time.time(),
    }
    record.update(
        {
            "original_outcome": decision.outcome,
            "outcome": "ready",
            "needs_human": False,
            "overridable": False,
            "question": "",
            "recommendation": "",
            "override": consumed,
            "human_review": True,
        }
    )
    record["audit"] = {
        **record["audit"],
        "outcome": "ready",
        "rule_ids": ["admin_quality_override"],
    }
    return True


def claim_next_task(
    *,
    mode: str,
    repo_context: Mapping[str, Any],
    state_dir: Path | None = None,
    predicate: Callable[[dict], bool] | None = None,
    mode_generation: int | None = None,
) -> AdmissionSelection | None:
    """鎖外評估排序後的 pending snapshot，再用 fingerprint CAS 原子提交與認領。

    ``shadow`` 永遠沿用既有可執行行為，只增加契約與裁決；``enforce`` 才依五向結果
    claim、park 或 complete。predicate 也在 flock 外執行，允許做唯讀 backlog 查詢。
    """
    if mode not in {"shadow", "enforce"}:
        raise ValueError("claim_next_task mode must be shadow or enforce")

    # 延遲 import 保持 evaluate() 可獨立測試，也避免 backlog 與本模組的 import cycle。
    from . import backlog

    if mode == "enforce" and admission_circuit_state(state_dir=state_dir).get("paused"):
        return None

    for _retry in range(3):
        try:
            snapshots = backlog.pending_snapshots(state_dir=state_dir)
            all_tasks = backlog.list_tasks(state_dir=state_dir)
        except Exception:  # noqa: BLE001 — storage adapter 失敗不可炸掉 worker
            if mode == "enforce":
                _record_admission_circuit("internal_error", state_dir=state_dir)
            return None
        if not snapshots:
            return None
        saw_candidate = False
        made_progress = False
        refresh_required = False
        for snapshot in snapshots:
            started = time.perf_counter()
            try:
                fingerprint = backlog.task_fingerprint(snapshot)
            except Exception:  # noqa: BLE001 — 無法 CAS 時不得繼續
                if mode == "enforce":
                    _record_admission_circuit("internal_error", state_dir=state_dir)
                return None
            try:
                if predicate is not None and not predicate(snapshot):
                    continue
                saw_candidate = True
                if mode == "enforce" and not _valid_repo_sha(repo_context.get("repo_sha")):
                    persisted, paused = _defer_internal_error(
                        snapshot,
                        fingerprint,
                        repo_context,
                        mode=mode,
                        phase="claim",
                        state_dir=state_dir,
                        started=started,
                        mode_generation=mode_generation,
                    )
                    made_progress = made_progress or persisted
                    if paused:
                        return None
                    continue
                context = build_local_context(snapshot, repo_context, tasks=all_tasks)
                decision = evaluate(snapshot, context, "claim")
                record = decision_record(decision, snapshot, mode=mode, phase="claim")
                override_consumed = _consume_valid_override(snapshot, decision, record)
            except Exception:  # noqa: BLE001 — evaluator/predicate 例外依 mode 保持契約
                if mode == "shadow":
                    selected = _shadow_claim_after_internal_error(
                        snapshot,
                        fingerprint,
                        repo_context,
                        state_dir=state_dir,
                        started=started,
                        mode_generation=mode_generation,
                    )
                    if selected is not None:
                        return selected
                    # CAS 衝突代表 snapshot 已過期；不可直接往下挑較低優先任務。
                    refresh_required = True
                    break
                else:
                    persisted, paused = _defer_internal_error(
                        snapshot,
                        fingerprint,
                        repo_context,
                        mode=mode,
                        phase="claim",
                        state_dir=state_dir,
                        started=started,
                        mode_generation=mode_generation,
                    )
                    made_progress = made_progress or persisted
                    if paused:
                        return None
                continue
            if mode == "enforce":
                circuit = admission_circuit_state(state_dir=state_dir)
                if circuit.get("paused"):
                    _defer_internal_error(
                        snapshot,
                        fingerprint,
                        repo_context,
                        mode=mode,
                        phase="claim",
                        state_dir=state_dir,
                        started=started,
                        mode_generation=mode_generation,
                    )
                    return None
            if (
                mode == "shadow"
                or override_consumed
                or decision.outcome in {"ready", "investigation"}
            ):
                transition = "claim"
            elif decision.outcome == "no_change":
                transition = "complete"
            else:
                transition = "park"
            try:
                committed, error = backlog.commit_admission(
                    snapshot["id"],
                    fingerprint,
                    contract=decision.contract.to_dict(),
                    admission=record,
                    transition=transition,
                    state_dir=state_dir,
                    expected_mode=mode,
                    expected_mode_generation=mode_generation,
                )
            except Exception:  # noqa: BLE001 — commit adapter 失敗不可放行
                if mode == "enforce":
                    _defer_internal_error(
                        snapshot,
                        fingerprint,
                        repo_context,
                        mode=mode,
                        phase="claim",
                        state_dir=state_dir,
                        started=started,
                        mode_generation=mode_generation,
                    )
                    return None
                return _shadow_legacy_claim(
                    snapshot,
                    fingerprint,
                    decision,
                    state_dir=state_dir,
                    mode_generation=mode_generation,
                )
            if committed is None:
                if error in {"conflict", "not_pending"}:
                    refresh_required = True
                    break
                return None
            made_progress = True
            if mode == "enforce":
                circuit = _record_admission_circuit(None, state_dir=state_dir)
                if circuit.get("paused"):
                    if transition == "claim":
                        _requeue_claim_after_circuit_failure(
                            committed,
                            repo_context,
                            mode=mode,
                            phase="claim",
                            state_dir=state_dir,
                            started=started,
                        )
                    return None
            _append_decision_audit(
                record,
                state_dir=state_dir,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            if transition == "claim":
                execution_task = dict(committed.task)
                execution_task["attempts"] = committed.attempts_before
                return AdmissionSelection(task=execution_task, decision=decision)
        if refresh_required:
            continue
        if not saw_candidate:
            return None
        if not made_progress:
            continue
        # 非 runnable 任務已原子收斂；刷新 snapshot 繼續找下一筆，不把它誤當 backlog 空。
    return None


def _admission_state_root(state_dir: Path | None) -> Path:
    if state_dir is not None:
        return state_dir
    from . import config

    return config.AUTOPILOT_STATE_DIR


def _admission_circuit_key(state_dir: Path | None) -> str:
    try:
        return str(_admission_state_root(state_dir).resolve())
    except (OSError, RuntimeError):
        return str(_admission_state_root(state_dir))


@contextlib.contextmanager
def _admission_circuit_lock(state_dir: Path | None):
    root = _admission_state_root(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "admission_circuit.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_admission_circuit_unlocked(*, state_dir: Path | None) -> dict[str, Any]:
    path = _admission_state_root(state_dir) / "admission_circuit.json"
    if not path.is_file():
        return {
            "version": 1,
            "consecutive_errors": 0,
            "paused": False,
            "notified": False,
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            isinstance(value, dict)
            and value.get("version") == 1
            and isinstance(value.get("consecutive_errors"), int)
            and not isinstance(value.get("consecutive_errors"), bool)
            and value.get("consecutive_errors") >= 0
            and isinstance(value.get("paused"), bool)
            and value.get("paused") is (value.get("consecutive_errors") >= 3)
        ):
            value["notified"] = bool(value.get("notified"))
            return value
    except (OSError, ValueError):
        pass
    return {
        "version": 1,
        "consecutive_errors": 3,
        "paused": True,
        "notified": False,
        "last_error": "circuit_state_invalid",
    }


def admission_circuit_state(*, state_dir: Path | None = None) -> dict[str, Any]:
    """讀 durable internal-error circuit；壞檔或鎖失敗皆 fail-closed 視為 paused。"""
    latched = _ADMISSION_CIRCUIT_LATCH.get(_admission_circuit_key(state_dir))
    if latched is not None:
        return dict(latched)
    try:
        with _admission_circuit_lock(state_dir):
            return _read_admission_circuit_unlocked(state_dir=state_dir)
    except Exception:  # noqa: BLE001 — circuit 本身不可讀時必須停
        return {
            "version": 1,
            "consecutive_errors": 3,
            "paused": True,
            "notified": False,
            "last_error": "circuit_state_unavailable",
        }


def mark_admission_circuit_notified(*, state_dir: Path | None = None) -> bool:
    """原子標記本輪熔斷已推播；只有第一個 caller 回 True。"""
    key = _admission_circuit_key(state_dir)
    latched = _ADMISSION_CIRCUIT_LATCH.get(key)
    if latched is not None:
        if not latched.get("paused") or latched.get("notified"):
            return False
        latched["notified"] = True
        _ADMISSION_CIRCUIT_LATCH[key] = latched
        return True
    try:
        from .secure_write import secure_write_root

        with _admission_circuit_lock(state_dir):
            value = _read_admission_circuit_unlocked(state_dir=state_dir)
            if not value.get("paused") or value.get("notified"):
                return False
            value["notified"] = True
            value["updated_at"] = time.time()
            path = _admission_state_root(state_dir) / "admission_circuit.json"
            secure_write_root(
                path,
                (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(),
            )
            return True
    except Exception:  # noqa: BLE001 — 無法 durable 去重時寧可不重複推播
        return False


def unmark_admission_circuit_notified(*, state_dir: Path | None = None) -> bool:
    """通知連 thread 都未啟動時撤回 claim，讓下一個 pause tick 可再試一次。"""
    key = _admission_circuit_key(state_dir)
    latched = _ADMISSION_CIRCUIT_LATCH.get(key)
    if latched is not None:
        if not latched.get("paused") or not latched.get("notified"):
            return False
        latched["notified"] = False
        _ADMISSION_CIRCUIT_LATCH[key] = latched
        return True
    try:
        from .secure_write import secure_write_root

        with _admission_circuit_lock(state_dir):
            value = _read_admission_circuit_unlocked(state_dir=state_dir)
            if not value.get("paused") or not value.get("notified"):
                return False
            value["notified"] = False
            value["updated_at"] = time.time()
            path = _admission_state_root(state_dir) / "admission_circuit.json"
            secure_write_root(
                path,
                (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(),
            )
            return True
    except Exception:  # noqa: BLE001 — 撤回失敗不改安全暫停，只失去本輪重試
        return False


_PUBLIC_AUDIT_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "contract_version",
        "contract_hash",
        "repo_sha",
        "phase",
        "outcome",
        "rule_ids",
        "missing_fields",
        "unresolved_target_count",
        "evidence_summary",
        "engine",
        "mode",
        "scope_hash",
        "needs_human",
        "overridable",
        "latency_ms",
        "cache_hit",
        "model_calls",
        "model",
        "token_usage",
        "model_error",
        "human_review",
        "override_reason_hash",
        "ts",
    }
)


def _public_audit_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """再次白名單投影，避免檔案被手改後 API 洩漏 raw prompt/output。"""
    return {key: record.get(key) for key in _PUBLIC_AUDIT_FIELDS if key in record}


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * 0.95) + 0.999) - 1))
    return round(ordered[index], 3)


def admission_audit_snapshot(
    *,
    limit: int = 100,
    days: int = 90,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """回傳去敏 audit、shadow 指標與 circuit；不重算 repo/模型。"""
    from . import jsonl_log

    root = _admission_state_root(state_dir)
    rows = [
        _public_audit_record(row)
        for row in jsonl_log.read_window(root / "admission_audit.jsonl", max(1, min(days, 365)))
    ]
    rows.sort(key=lambda row: float(row.get("ts") or 0), reverse=True)
    total = len(rows)
    no_model = [row for row in rows if int(row.get("model_calls") or 0) == 0]
    model = [row for row in rows if int(row.get("model_calls") or 0) > 0]
    cache_hits = sum(bool(row.get("cache_hit")) for row in rows)
    outcomes = Counter(str(row.get("outcome") or "unknown") for row in rows)
    metrics = {
        "total": total,
        "outcomes": dict(sorted(outcomes.items())),
        "no_llm_rate": round(len(no_model) / total, 3) if total else None,
        "cache_hit_rate": round(cache_hits / total, 3) if total else None,
        "p95_no_llm_ms": _p95([float(row.get("latency_ms") or 0) for row in no_model]),
        "p95_with_llm_ms": _p95([float(row.get("latency_ms") or 0) for row in model]),
        "internal_errors": sum(
            "admission_internal_error" in (row.get("rule_ids") or []) for row in rows
        ),
        "human_reviews": sum(bool(row.get("human_review")) for row in rows),
        # 這兩項需 10-task 人工標註 replay 才能誠實計算，未知不可灌成 100%/0。
        "ready_precision": None,
        "human_false_blocks": None,
        "unauthorized_high_risk_passes": None,
    }
    return {
        "records": rows[: max(1, min(int(limit), 500))],
        "metrics": metrics,
        "circuit": admission_circuit_state(state_dir=state_dir),
    }


def _record_admission_circuit(
    error: str | None,
    *,
    state_dir: Path | None,
) -> dict[str, Any]:
    key = _admission_circuit_key(state_dir)
    latched = _ADMISSION_CIRCUIT_LATCH.get(key)
    if latched is not None and latched.get("paused"):
        return dict(latched)
    try:
        from .secure_write import secure_write_root

        with _admission_circuit_lock(state_dir):
            previous = _read_admission_circuit_unlocked(state_dir=state_dir)
            # 熔斷只能由明確人工復原／移除 durable state 解鎖；新 enqueue 成功或錯誤
            # 都不得把已 paused 的安全狀態悄悄降級。
            if previous.get("paused"):
                return previous
            count = int(previous.get("consecutive_errors") or 0) + 1 if error else 0
            paused = count >= 3
            value = {
                "version": 1,
                "consecutive_errors": count,
                "paused": paused,
                "notified": bool(previous.get("notified"))
                if paused and previous.get("paused")
                else False,
                "last_error": str(error or "")[:100],
                "updated_at": time.time(),
            }
            path = _admission_state_root(state_dir) / "admission_circuit.json"
            secure_write_root(
                path,
                (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(),
            )
            _ADMISSION_CIRCUIT_LATCH.pop(key, None)
    except Exception:  # noqa: BLE001 — circuit 落檔壞掉時呼叫端仍依目前結果 fail-closed
        value = {
            "version": 1,
            "consecutive_errors": 3,
            "paused": True,
            "notified": False,
            "last_error": "circuit_write_failed",
            "updated_at": time.time(),
        }
        _ADMISSION_CIRCUIT_LATCH[key] = dict(value)
    return value


async def claim_next_task_with_semantic_fallback(
    *,
    mode: str,
    repo_context: Mapping[str, Any],
    resolver: Any,
    cache_dir: Path,
    state_dir: Path | None = None,
    predicate: Callable[[dict], bool] | None = None,
    timeout_s: float = 30.0,
    mode_generation: int | None = None,
) -> AdmissionSelection | None:
    """claim coordinator 的 async 版本；語意缺口至多用一次 fast resolver。"""
    if mode not in {"shadow", "enforce"}:
        raise ValueError("claim mode must be shadow or enforce")
    from . import backlog

    if mode == "enforce" and admission_circuit_state(state_dir=state_dir).get("paused"):
        return None
    decision_excluded_ids: set[Any] = set()
    for _retry in range(3):
        try:
            snapshots = backlog.pending_snapshots(state_dir=state_dir)
            all_tasks = backlog.list_tasks(state_dir=state_dir)
        except Exception:  # noqa: BLE001 — 儲存 adapter 失敗時所有 task 維持原狀
            if mode == "enforce":
                _record_admission_circuit("internal_error", state_dir=state_dir)
            return None
        if not snapshots:
            return None
        saw_candidate = False
        made_progress = False
        refresh_required = False
        for snapshot in snapshots:
            if snapshot.get("id") in decision_excluded_ids:
                continue
            started = time.perf_counter()
            try:
                fingerprint = backlog.task_fingerprint(snapshot)
            except Exception:  # noqa: BLE001 — 非 JSON snapshot 不可能安全 CAS
                if mode == "enforce":
                    _record_admission_circuit("internal_error", state_dir=state_dir)
                return None
            try:
                matches = predicate is None or predicate(snapshot)
            except Exception:  # noqa: BLE001 — predicate 也是 admission adapter 的一部分
                if mode == "shadow":
                    return None
                persisted, paused = _defer_internal_error(
                    snapshot,
                    fingerprint,
                    repo_context,
                    mode=mode,
                    phase="claim",
                    state_dir=state_dir,
                    started=started,
                    mode_generation=mode_generation,
                )
                made_progress = made_progress or persisted
                if paused:
                    return None
                continue
            if not matches:
                continue
            saw_candidate = True
            if mode == "enforce" and not _valid_repo_sha(repo_context.get("repo_sha")):
                persisted, paused = _defer_internal_error(
                    snapshot,
                    fingerprint,
                    repo_context,
                    mode=mode,
                    phase="claim",
                    state_dir=state_dir,
                    started=started,
                    mode_generation=mode_generation,
                )
                made_progress = made_progress or persisted
                if paused:
                    return None
                continue
            try:
                pre_context = build_local_context(snapshot, repo_context, tasks=all_tasks)
                pre_decision = evaluate(snapshot, pre_context, "claim")
                pre_record = decision_record(
                    pre_decision,
                    snapshot,
                    mode=mode,
                    phase="claim",
                )
                override_consumed = _consume_valid_override(
                    snapshot,
                    pre_decision,
                    pre_record,
                )
                if override_consumed:
                    decision = pre_decision
                    record = pre_record
                    model_meta = {
                        "cache_hit": False,
                        "model_calls": 0,
                        "model": None,
                        "token_usage": None,
                        "error": "",
                    }
                else:
                    decision, model_meta = await evaluate_with_semantic_fallback(
                        snapshot,
                        repo_context,
                        "claim",
                        resolver=resolver,
                        cache_dir=cache_dir,
                        tasks=all_tasks,
                        timeout_s=timeout_s,
                    )
                    record = decision_record(decision, snapshot, mode=mode, phase="claim")
            except Exception:  # noqa: BLE001 — 所有 adapter/evaluator 例外統一 fail-closed
                if mode == "shadow":
                    selected = _shadow_claim_after_internal_error(
                        snapshot,
                        fingerprint,
                        repo_context,
                        state_dir=state_dir,
                        started=started,
                        mode_generation=mode_generation,
                    )
                    if selected is not None:
                        return selected
                    return None
                persisted, paused = _defer_internal_error(
                    snapshot,
                    fingerprint,
                    repo_context,
                    mode=mode,
                    phase="claim",
                    state_dir=state_dir,
                    started=started,
                    mode_generation=mode_generation,
                )
                made_progress = made_progress or persisted
                if paused:
                    return None
                continue
            if predicate is not None:
                try:
                    decided_task = {
                        **snapshot,
                        "contract": decision.contract.to_dict(),
                        "admission": {"outcome": decision.outcome},
                    }
                    if not predicate(decided_task):
                        decision_excluded_ids.add(snapshot.get("id"))
                        continue
                except Exception:  # noqa: BLE001 — post-decision router 也須 fail-closed
                    if mode == "shadow":
                        return None
                    persisted, paused = _defer_internal_error(
                        snapshot,
                        fingerprint,
                        repo_context,
                        mode=mode,
                        phase="claim",
                        state_dir=state_dir,
                        started=started,
                        mode_generation=mode_generation,
                    )
                    made_progress = made_progress or persisted
                    if paused:
                        return None
                    continue
            record.update(
                {
                    "engine": (
                        "semantic+deterministic"
                        if model_meta["model_calls"] or model_meta["cache_hit"]
                        else "deterministic"
                    ),
                    "cache_hit": model_meta["cache_hit"],
                    "model_calls": model_meta["model_calls"],
                    "model": model_meta["model"],
                    "token_usage": model_meta["token_usage"],
                    "model_error": model_meta["error"],
                }
            )
            if model_meta["error"]:
                record.update(
                    {
                        "outcome": "blocked",
                        "reasons": ["admission_internal_error"],
                        "needs_human": False,
                        "overridable": False,
                        "question": "",
                        "recommendation": "",
                        "timeout_default": "park",
                    }
                )
                record["audit"] = {
                    **record["audit"],
                    "outcome": "blocked",
                    "rule_ids": ["admission_internal_error"],
                }
                if mode == "shadow":
                    try:
                        committed, error = backlog.commit_admission(
                            snapshot["id"],
                            fingerprint,
                            contract=decision.contract.to_dict(),
                            admission=record,
                            transition="claim",
                            state_dir=state_dir,
                            expected_mode=mode,
                            expected_mode_generation=mode_generation,
                        )
                    except Exception:  # noqa: BLE001 — observer 寫入壞掉仍沿用舊 claim
                        return _shadow_legacy_claim(
                            snapshot,
                            fingerprint,
                            decision,
                            state_dir=state_dir,
                            mode_generation=mode_generation,
                        )
                    if committed is None:
                        if error in {"conflict", "not_pending"}:
                            refresh_required = True
                            break
                        return None
                    _append_decision_audit(
                        record,
                        state_dir=state_dir,
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )
                    execution_task = dict(committed.task)
                    execution_task["attempts"] = committed.attempts_before
                    return AdmissionSelection(task=execution_task, decision=decision)
                try:
                    committed, error = backlog.commit_admission(
                        snapshot["id"],
                        fingerprint,
                        contract=decision.contract.to_dict(),
                        admission=record,
                        transition="record",
                        retry_after=time.time() + 60.0,
                        state_dir=state_dir,
                        expected_mode=mode,
                        expected_mode_generation=mode_generation,
                    )
                except Exception:  # noqa: BLE001 — 寫入 adapter 例外同樣不可放行
                    persisted, paused = _defer_internal_error(
                        snapshot,
                        fingerprint,
                        repo_context,
                        mode=mode,
                        phase="claim",
                        state_dir=state_dir,
                        started=started,
                        mode_generation=mode_generation,
                    )
                    made_progress = made_progress or persisted
                    if paused:
                        return None
                    continue
                if committed is None:
                    if error in {"conflict", "not_pending"}:
                        refresh_required = True
                        break
                    return None
                made_progress = True
                _append_decision_audit(
                    record,
                    state_dir=state_dir,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                circuit = _record_admission_circuit(
                    model_meta["error"],
                    state_dir=state_dir,
                )
                if circuit["paused"]:
                    return None
                continue
            circuit = (
                admission_circuit_state(state_dir=state_dir)
                if mode == "enforce"
                else {"paused": False}
            )
            if circuit.get("paused"):
                persisted, _paused = _defer_internal_error(
                    snapshot,
                    fingerprint,
                    repo_context,
                    mode=mode,
                    phase="claim",
                    state_dir=state_dir,
                    started=started,
                    mode_generation=mode_generation,
                )
                made_progress = made_progress or persisted
                return None
            if (
                mode == "shadow"
                or override_consumed
                or decision.outcome in {"ready", "investigation"}
            ):
                transition = "claim"
            elif decision.outcome == "no_change":
                transition = "complete"
            else:
                transition = "park"
            try:
                committed, error = backlog.commit_admission(
                    snapshot["id"],
                    fingerprint,
                    contract=decision.contract.to_dict(),
                    admission=record,
                    transition=transition,
                    state_dir=state_dir,
                    expected_mode=mode,
                    expected_mode_generation=mode_generation,
                )
            except Exception:  # noqa: BLE001 — 寫入 adapter 例外同樣不可放行
                if mode == "shadow":
                    return _shadow_legacy_claim(
                        snapshot,
                        fingerprint,
                        decision,
                        state_dir=state_dir,
                        mode_generation=mode_generation,
                    )
                persisted, paused = _defer_internal_error(
                    snapshot,
                    fingerprint,
                    repo_context,
                    mode=mode,
                    phase="claim",
                    state_dir=state_dir,
                    started=started,
                    mode_generation=mode_generation,
                )
                made_progress = made_progress or persisted
                if paused:
                    return None
                continue
            if committed is None:
                if error in {"conflict", "not_pending"}:
                    refresh_required = True
                    break
                return None
            made_progress = True
            if mode == "enforce":
                circuit = _record_admission_circuit(None, state_dir=state_dir)
                if circuit.get("paused"):
                    if transition == "claim":
                        _requeue_claim_after_circuit_failure(
                            committed,
                            repo_context,
                            mode=mode,
                            phase="claim",
                            state_dir=state_dir,
                            started=started,
                        )
                    return None
            _append_decision_audit(
                record,
                state_dir=state_dir,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            if transition == "claim":
                execution_task = dict(committed.task)
                execution_task["attempts"] = committed.attempts_before
                return AdmissionSelection(task=execution_task, decision=decision)
        if refresh_required:
            continue
        if not saw_candidate:
            return None
        if not made_progress:
            continue
    return None
