"""AST rules: no bare asyncio.create_task outside app/tasks.py.

Background work starts through tasks.spawn(), which supervises restarts
and logs crashes — a bare create_task escapes both.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"
ALLOWED = {APP_ROOT / "tasks.py"}


def _create_task_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text())
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_task"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
        ):
            hits.append(node.lineno)
    return hits


def test_no_bare_create_task_outside_spawn() -> None:
    offenders = []
    for path in APP_ROOT.rglob("*.py"):
        if path in ALLOWED:
            continue
        for line in _create_task_calls(path):
            offenders.append(f"{path.relative_to(APP_ROOT)}:{line}")
    assert offenders == [], (
        "bare asyncio.create_task outside app/tasks.py — use tasks.spawn():\n"
        + "\n".join(offenders)
    )
