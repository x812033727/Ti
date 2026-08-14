from __future__ import annotations

import sys
import types

from studio.fake_experts import _CALCULATOR_PY, _MAIN_PY


def _load_generated_main(monkeypatch):
    calculator_mod = types.ModuleType("calculator")
    exec(_CALCULATOR_PY, calculator_mod.__dict__)
    monkeypatch.setitem(sys.modules, "calculator", calculator_mod)

    main_mod = types.ModuleType("main")
    exec(_MAIN_PY, main_mod.__dict__)
    return main_mod


def test_fake_cli_div_zero_matches_readme(monkeypatch, capsys):
    main_mod = _load_generated_main(monkeypatch)

    assert main_mod.main(["div", "1", "0"]) == 1
    assert capsys.readouterr().out.strip() == "除數不可為 0"
