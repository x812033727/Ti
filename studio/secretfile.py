"""安全寫入秘密 .env 檔：保證檔案權限精確為 0600，且與行程 umask 脫鉤。

為什麼需要這層：
- `dotenv.set_key()` 對「既存檔」會主動 `os.chmod(dest, original_mode)` 把舊權限
  （例如曾被以 0644 建立）還原回去，再原子 replace，故不保證收緊到 0600。
- 直接用 `os.open(path, ..., 0o600)` 仍會被行程 umask 遮罩，且多執行緒下用
  「暫存 umask → open → 還原」是行程全域、非執行緒安全的寫法，會開出 world-readable 窗口。

做法（不依賴 umask）：
  ① 確保父目錄存在
  ② first-touch：若檔不存在，以 `os.open(O_CREAT|O_EXCL|O_WRONLY, 0o600)`+`fchmod(fd,0o600)`
     原子建出空檔——對齊「不依賴 umask 的精確 0600 建檔 / TOCTOU 防護」。
     注意：這不是內容權限的最終依據（set_key 底層走 NamedTemporaryFile+os.replace，
     會換掉 inode），內容權限由 ④ 的 os.chmod 保證。
  ③ `set_key(path, key, value, follow_symlinks=False)` 寫入內容，保留 dotenv 的引號／escape 相容。
  ④ `os.chmod(path, 0o600)` 收尾收緊（普通 chmod：本平台 os.chmod 不支援 follow_symlinks）。
     在父目錄可信的前提下，replace 後為 regular file，無 symlink 風險；若攻擊者已能寫入
     父目錄，則屬超出本函式威脅模型的更嚴重妥協。

併發：模組級 threading.Lock 序列化同行程多執行緒寫入，消除權限窗口與 .env 內容互蓋
（lost update）；跨程序原子性由 set_key 底層的 NamedTemporaryFile+os.replace 保證。

注意：`path` 由呼叫端控制，本函式不負責路徑驗證；呼叫端勿傳入未經驗證的外部路徑。
"""

from __future__ import annotations

import os
import threading

from dotenv import set_key

_lock = threading.Lock()


def write_secret_file(path: str, key: str, value: str) -> None:
    """安全地把 key=value 寫進 path 指定的 .env 檔，並保證該檔權限為 0600。

    僅負責「安全寫檔 + 收緊權限」；os.environ / config 的同步由呼叫端各自處理。
    """
    with _lock:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # ② first-touch 原子建檔：精確 0600、與 umask 脫鉤、防 symlink/TOCTOU 競爭。
        # O_EXCL 讓既存檔（含 symlink）直接 EEXIST，交由 ③④ 處理——跨程序首次競建也安全。
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        except FileExistsError:
            pass  # 已存在，內容權限由 ③④ 保證

        # ③ 寫入內容（保留 dotenv 格式相容；不跟隨 symlink）。
        set_key(path, key, value, follow_symlinks=False)

        # ④ 收尾收緊：set_key 對既存檔會還原舊權限，這步是 0600 的最終保證。
        os.chmod(path, 0o600)
