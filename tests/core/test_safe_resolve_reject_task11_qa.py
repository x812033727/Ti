"""QA 獨立驗證（任務 #11 / 驗收標準 2）：safe_resolve 對 5 類惡意輸入一律回 None。

每類都用多個變體加壓，並確認『回 None 而非拋例外』（呼叫端依賴 None 判斷）。
5 類：含 .. 的相對路徑、絕對路徑、外部 symlink、不存在的檔案、symlink loop。
"""

from __future__ import annotations

import pytest

from studio import workspace


@pytest.fixture
def root(tmp_path):
    (tmp_path / "ws").mkdir()
    return tmp_path / "ws"


# --- (1) 含 .. 的相對路徑 ---


@pytest.mark.parametrize(
    "rel",
    [
        "../evil.txt",
        "../../etc/passwd",
        "a/../../evil.txt",
        "sub/../../escape",
        "./../x",
        "..",
        "a/b/../../../out",
    ],
)
def test_dotdot_returns_none(root, rel):
    assert workspace.safe_resolve(root, rel) is None
    # must_exist=False 也一樣擋（fail-fast 在 resolve 之前）
    assert workspace.safe_resolve(root, rel, must_exist=False) is None


# --- (2) 絕對路徑 ---


@pytest.mark.parametrize("rel", ["/etc/passwd", "/", "/tmp/x", "//double/abs"])
def test_absolute_returns_none(root, rel):
    assert workspace.safe_resolve(root, rel) is None
    assert workspace.safe_resolve(root, rel, must_exist=False) is None


# --- (3) 指向 workspace 外的 symlink ---


def test_external_file_symlink_returns_none(root):
    secret = root.parent / "secret.txt"
    secret.write_text("S", encoding="utf-8")
    (root / "leak").symlink_to(secret)
    assert workspace.safe_resolve(root, "leak") is None


def test_external_dir_symlink_then_file_returns_none(root):
    outside = root.parent / "outdir"
    outside.mkdir()
    (outside / "f.txt").write_text("S", encoding="utf-8")
    (root / "linkdir").symlink_to(outside)
    # 經由外部目錄 symlink 觸及其中的檔案 → 解析後落在 root 外 → None
    assert workspace.safe_resolve(root, "linkdir/f.txt") is None


def test_symlink_to_parent_returns_none(root):
    (root / "up").symlink_to(root.parent)
    assert workspace.safe_resolve(root, "up") is None


# --- (4) 不存在的檔案（must_exist=True） ---


@pytest.mark.parametrize("rel", ["nope.txt", "missing/deep.py", "a/b/c.txt"])
def test_missing_returns_none(root, rel):
    assert workspace.safe_resolve(root, rel) is None


# --- (5) symlink loop ---


def test_symlink_loop_returns_none(root):
    (root / "a").symlink_to(root / "b")
    (root / "b").symlink_to(root / "a")
    assert workspace.safe_resolve(root, "a") is None
    assert workspace.safe_resolve(root, "b") is None


def test_self_symlink_loop_returns_none(root):
    (root / "self").symlink_to(root / "self")
    assert workspace.safe_resolve(root, "self") is None


# --- 共同性質：永遠回 None，絕不拋例外 ---


@pytest.mark.parametrize(
    "rel",
    ["../x", "/abs", "nope", "a/../../b", ".."],
)
def test_never_raises_returns_none_or_path(root, rel):
    try:
        result = workspace.safe_resolve(root, rel)
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"safe_resolve 不應拋例外，rel={rel!r}: {e!r}")
    assert result is None
