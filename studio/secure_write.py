"""root-only state 檔的安全寫入唯一收斂點（choke point）。

所有「只應由 root 持有」的 state 寫入（history 的 meta/events、backlog.json）都必須經由
secure_write_root，集中套用同一套保護：

  - 原子寫入：永遠寫到同目錄新建的暫存檔，fsync 後 os.rename 取代目標（失敗不破壞既有檔）。
  - 反 symlink TOCTOU：暫存檔以 O_CREAT|O_EXCL|O_NOFOLLOW 開啟，rename 取代目標路徑本身，
    絕不跟著 symlink 改寫到其指向的 victim。
  - 擁有者強制驗證：chown 成 root 後，不信任 chown 回傳值，改以開啟中的 fd 做 fstat 複驗
    （uid==0 且 nlink==1，擋 hardlink）。
  - 三態 fail 策略（require_chown）：
      strict — 驗證未過即 raise SecureWriteError（fail-closed），不留半成品。
      warn   — 驗證未過記 WARNING 後放行（過渡選項）。
      off    — 完全略過 chown/驗證，靜默放行（顯式逃生開關）。
    require_chown=None 時採 config.require_chown_mode()（預設 strict）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

_MODES = ("strict", "warn", "off")


class SecureWriteError(Exception):
    """root-only 寫入未通過擁有者強制驗證（strict fail-closed）。"""


def _resolve_mode(require_chown: str | None) -> str:
    """決定有效模式：None → config 預設；不認得的值 → fail-safe 取 strict。"""
    mode = config.require_chown_mode() if require_chown is None else require_chown
    return mode if mode in _MODES else "strict"


def secure_write_root(
    path,
    data: bytes,
    *,
    mode: int = 0o600,
    require_chown: str | None = None,
) -> None:
    """以 root-only 保護把 data 原子寫入 path。

    成功時無回傳；strict 驗證未過時 raise SecureWriteError（絕不靜默成功）。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"secure_write_root 只收 bytes，收到 {type(data).__name__}")

    target = Path(os.fspath(path))
    eff = _resolve_mode(require_chown)
    parent = target.parent
    os.makedirs(parent, exist_ok=True)

    # 暫存檔與目標同目錄（確保 rename 為同檔系統的原子操作）。
    tmp = parent / f".{target.name}.{os.getpid()}.tmp"
    # O_EXCL 保證新建（不接管殘留檔）、O_NOFOLLOW 擋 symlink 預埋。
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, mode)
    try:
        os.fchmod(fd, mode)  # 不受 umask 影響，確保權限就是 mode（如 0o600）

        # 完整寫入（處理 short write；無進展即視為 IO 失敗，不留半截檔）。
        view = memoryview(bytes(data))
        written = 0
        total = len(view)
        while written < total:
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError(f"secure_write_root：寫入無進展，放棄 {target}")
            written += n
        os.fsync(fd)

        if eff in ("strict", "warn"):
            try:
                os.fchown(fd, 0, 0)  # 強制 root:root
            except OSError as e:
                if eff == "strict":
                    raise SecureWriteError(
                        f"chown 失敗，無法確保 {target} 為 root 擁有：{e}"
                    ) from e
                logger.warning("require_chown=warn：chown 失敗，仍放行寫入 %s（%s）", target, e)
            else:
                if eff == "strict":
                    st = os.fstat(fd)
                    if st.st_uid != 0:
                        raise SecureWriteError(
                            f"擁有者驗證失敗：{target} 期望 uid 0，實得 {st.st_uid}"
                        )
                    if st.st_nlink != 1:
                        raise SecureWriteError(
                            f"hardlink 驗證失敗：{target} st_nlink={st.st_nlink}（疑似硬連結）"
                        )

        os.close(fd)
        fd = -1
        os.rename(tmp, target)  # 原子取代目標路徑本身（symlink 也被換掉，不波及 victim）
    except BaseException:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)  # 失敗不留暫存半成品
        except OSError:
            pass
        raise
