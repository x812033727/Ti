"""任務 #2 契約：studio/secure_write.py 的 secure_write_root 核心行為。

對應驗收標準 #2/#3/#4：
- 公開 API：SecureWriteError、secure_write_root(path, data, *, mode=0o600, require_chown=None)。
- 三態 fail-closed：strict→chown 失敗／owner≠0／nlink≠1 皆 raise 且不留目標檔與 tmp；
  warn→放行但記 warning；off→不呼叫 fchown、靜默放行。
- 非 bytes → TypeError；short-write 迴圈補寫至完整；os.write 回 0 → OSError 不留半截檔；
  symlink 目標不被波及（rename 取代）。

owner 相關斷言以 monkeypatch `secure_write.os`（fchown/fstat）模擬，root/非 root 皆確定性通過。
"""

from __future__ import annotations

import os
import types

import pytest

from studio import secure_write
from studio.secure_write import SecureWriteError, secure_write_root


def _fake_stat(uid=0, nlink=1):
    return types.SimpleNamespace(st_uid=uid, st_nlink=nlink)


def _patch_root(monkeypatch, uid=0, nlink=1):
    """模擬 fchown 成功、fstat 回指定 owner/nlink（root 成功路徑）。"""
    monkeypatch.setattr(secure_write.os, "fchown", lambda fd, u, g: None)
    monkeypatch.setattr(secure_write.os, "fstat", lambda fd: _fake_stat(uid, nlink))


def _leftovers(d, name):
    return [p.name for p in d.iterdir() if name in p.name and p.name != name]


# ---- 公開 API 形狀 ----


def test_public_api_exists():
    assert issubclass(SecureWriteError, Exception)
    import inspect

    sig = inspect.signature(secure_write_root)
    assert "mode" in sig.parameters
    assert "require_chown" in sig.parameters
    assert sig.parameters["require_chown"].default is None


def test_uses_os_module_attribute(monkeypatch, tmp_path):
    """secure_write.os 為模組屬性，monkeypatch 可生效（研究員指定的契約）。"""
    called = {}
    real_open = secure_write.os.open

    def spy_open(path, flags, mode=0o777):
        called["open"] = True
        return real_open(path, flags, mode)

    monkeypatch.setattr(secure_write.os, "open", spy_open)
    secure_write_root(tmp_path / "a.json", b"x", require_chown="off")
    assert called.get("open")


# ---- 入參型別 ----


@pytest.mark.parametrize("bad", ["str", 123, None, {"k": 1}])
def test_non_bytes_raises_typeerror(tmp_path, bad):
    with pytest.raises(TypeError):
        secure_write_root(tmp_path / "a", bad, require_chown="off")


def test_bytearray_accepted(tmp_path):
    secure_write_root(tmp_path / "a", bytearray(b"hi"), require_chown="off")
    assert (tmp_path / "a").read_bytes() == b"hi"


# ---- off 模式 ----


def test_off_skips_fchown(monkeypatch, tmp_path):
    monkeypatch.setattr(secure_write.os, "fchown", lambda *a: pytest.fail("off 不應呼叫 fchown"))
    secure_write_root(tmp_path / "off.json", b"data", require_chown="off")
    assert (tmp_path / "off.json").read_bytes() == b"data"


# ---- strict 成功路徑（模擬 root）----


def test_strict_success_no_leftover(monkeypatch, tmp_path):
    _patch_root(monkeypatch)
    target = tmp_path / "s.json"
    secure_write_root(target, b"ok", require_chown="strict")
    assert target.read_bytes() == b"ok"
    assert _leftovers(tmp_path, "s.json") == []


def test_strict_sets_mode_0600(monkeypatch, tmp_path):
    _patch_root(monkeypatch)
    target = tmp_path / "m.json"
    secure_write_root(target, b"x", require_chown="strict")
    import stat

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


# ---- strict 失敗路徑：皆 raise 且不留目標檔與 tmp ----


def test_strict_fchown_fail_raises_and_cleans(monkeypatch, tmp_path):
    def boom(fd, u, g):
        raise OSError("EPERM")

    monkeypatch.setattr(secure_write.os, "fchown", boom)
    target = tmp_path / "f.json"
    with pytest.raises(SecureWriteError):
        secure_write_root(target, b"x", require_chown="strict")
    assert not target.exists()
    assert _leftovers(tmp_path, "f.json") == []


def test_strict_wrong_owner_raises(monkeypatch, tmp_path):
    _patch_root(monkeypatch, uid=1000)  # owner 非 root
    target = tmp_path / "o.json"
    with pytest.raises(SecureWriteError) as ei:
        secure_write_root(target, b"x", require_chown="strict")
    assert "1000" in str(ei.value)  # 訊息含實際 uid
    assert not target.exists()
    assert _leftovers(tmp_path, "o.json") == []


def test_strict_nlink_not_one_raises(monkeypatch, tmp_path):
    _patch_root(monkeypatch, nlink=2)  # 疑似 hardlink
    target = tmp_path / "h.json"
    with pytest.raises(SecureWriteError) as ei:
        secure_write_root(target, b"x", require_chown="strict")
    assert "nlink" in str(ei.value)
    assert not target.exists()


# ---- warn 模式：fchown 失敗仍放行並記 warning ----


def test_warn_fchown_fail_passes(monkeypatch, tmp_path, caplog):
    def boom(fd, u, g):
        raise OSError("EPERM")

    monkeypatch.setattr(secure_write.os, "fchown", boom)
    import logging

    with caplog.at_level(logging.WARNING):
        secure_write_root(tmp_path / "w.json", b"w", require_chown="warn")
    assert (tmp_path / "w.json").read_bytes() == b"w"
    assert any("fchown" in r.message or "warn" in r.message.lower() for r in caplog.records)


# ---- short-write 迴圈 ----


def test_short_write_loop_writes_all(monkeypatch, tmp_path):
    real_write = os.write

    def one_byte(fd, b):
        return real_write(fd, b[:1])  # 每次只寫 1 byte

    monkeypatch.setattr(secure_write.os, "write", one_byte)
    payload = b"abcdefghij" * 5
    secure_write_root(tmp_path / "sw.json", payload, require_chown="off")
    assert (tmp_path / "sw.json").read_bytes() == payload


def test_os_write_zero_raises_no_halffile(monkeypatch, tmp_path):
    monkeypatch.setattr(secure_write.os, "write", lambda fd, b: 0)
    target = tmp_path / "z.json"
    with pytest.raises(OSError):
        secure_write_root(target, b"data", require_chown="off")
    assert not target.exists()
    assert _leftovers(tmp_path, "z.json") == []


# ---- symlink 防護：rename 取代，不波及 symlink 指向的真實檔 ----


def test_symlink_target_not_touched(tmp_path):
    real = tmp_path / "real.txt"
    real.write_bytes(b"original")
    link = tmp_path / "link.json"
    os.symlink(real, link)
    secure_write_root(link, b"new", require_chown="off")
    assert real.read_bytes() == b"original"  # 真實檔未被波及
    assert not os.path.islink(link)  # symlink 被 rename 取代為普通檔
    assert link.read_bytes() == b"new"
