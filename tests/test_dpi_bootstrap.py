from __future__ import annotations

import ast
from pathlib import Path


def test_dpi_awareness_is_enabled_before_wx_import():
    action_path = Path(__file__).parents[1] / "kilog_action.py"
    module = ast.parse(action_path.read_text(encoding="utf-8"))

    dpi_call_line = next(
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_enable_high_dpi"
    )
    wx_import_line = next(
        node.lineno
        for node in module.body
        if isinstance(node, ast.Import)
        and any(alias.name == "wx" for alias in node.names)
    )

    assert dpi_call_line < wx_import_line
