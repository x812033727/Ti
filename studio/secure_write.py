"""root-only 安全寫入：原子取代 + symlink 防護 + owner 驗證。

state 檔案（history meta/events、backlog.json）以此寫入，確保只有 root 能改：

  同目錄 tmp（O_EXCL|O_NOFOLLOW 原子建檔）→ loop write 防 short-write →
  fchown(0,0) → fstat 驗 owner==0 且 nlink==1 → rename 原子取代目標。

任一步失敗即清掉 tmp、不留半截檔；strict 模式驗證不過直接 raise SecureWriteError。
三態（strict/warn/off）取自 config.require_chown_mode()，呼叫端亦可用 require_chown
參數強制覆蓋（會記 warning 留稽核軌跡）。

monkeypatch 友善：所有 os 操作走模組屬性 `secure_write.os`，config 走 `secure_write.config`。
"""

from __future__ import annotations

import logging
import os

from . import config

logger = logging.getLogger("ti.secure_write")


class SecureWriteError(Exception):
    """安全寫入驗證失敗：chown 失敗、owner 非 root（uid≠0）、或 nlink≠1。"""


def secure_write_root(path, data, *, mode: int = 0o600, require_chown=None) -> None:
    """原子寫入 bytes `data` 到 `path`，並依模式驗證檔案 owner 為 root。

    path: 目標路徑（str 或 PathLike）。
    data: bytes／bytearray（其餘型別拋 TypeError）。
    mode: 建立 tmp 檔的權限（預設 0o600）。
    require_chown: None＝讀 config.require_chown_mode()；非 None＝呼叫端強制覆蓋三態
      （安全邊界旁路，會記 warning 留稽核軌跡）。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"secure_write_root 需要 bytes，收到 {type(data).__name__}")

    if require_chown is None:
        chown_mode = config.require_chown_mode()
    else:
        chown_mode = str(require_chown)
        logger.warning(
            "secure_write_root: require_chown 被呼叫端強制覆蓋為 %s，路徑 %s", chown_mode, path
        )

    target = os.fspath(path)
    parent = os.path.dirname(target) or "."
    base = os.path.basename(target)
    # tmp 命名含 pid + 隨機字尾：同進程多 thread/coroutine 對同一 path 並發時不碰撞；
    # O_EXCL 保證萬一碰撞則 open 失敗（不靜默覆蓋）。前綴 "." 不影響目標 glob 清理。
    tmp = os.path.join(parent, f".{base}.{os.getpid()}.{os.urandom(4).hex()}")

    flags = os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(tmp, flags, mode)
    try:
        # loop write 防 short-write：os.write 可能未一次寫完。
        view = bytes(data)
        written = 0
        while written < len(view):
            n = os.write(fd, view[written:])
            if n == 0:
                raise OSError("os.write 回傳 0，寫入未前進")
            written += n

        if chown_mode != "off":
            try:
                os.fchown(fd, 0, 0)
            except OSError as e:
                if chown_mode == "strict":
                    raise SecureWriteError(f"fchown(0,0) 失敗（非 root？）：{e}") from e
                # warn：已知可能非 root、顯式接受——記警告後放行，不再做 fstat（語意一致）。
                logger.warning(
                    "secure_write_root: fchown 失敗（warn 放行）：%s 路徑 %s", e, target
                )
            else:
                if chown_mode == "strict":
                    st = os.fstat(fd)
                    if st.st_uid != 0:
                        raise SecureWriteError(
                            f"owner 非 root（uid={st.st_uid}），拒絕落地：{target}"
                        )
                    if st.st_nlink != 1:
                        raise SecureWriteError(
                            f"nlink={st.st_nlink}≠1（疑似 hardlink 攻擊），拒絕落地：{target}"
                        )

        os.close(fd)
        fd = None
        os.rename(tmp, target)  # POSIX 同 filesystem 原子取代（含取代既有 symlink 本身）
    except BaseException:
        # 失敗清理：close 與 unlink 各自獨立 try，確保「關 fd」即使拋例外也不阻擋「刪 tmp」。
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
