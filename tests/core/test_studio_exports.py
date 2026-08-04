"""studio package-level 匯出契約守護。"""

import importlib

import studio
from studio import secure_write


def test_secure_write_is_explicit_package_export():
    direct = importlib.import_module("studio.secure_write")

    assert secure_write is direct
    assert studio.secure_write is direct
