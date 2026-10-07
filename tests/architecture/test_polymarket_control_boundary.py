from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONTROL = ROOT / "src/jev_trading_lab/polymarket/control.py"


def test_control_has_no_research_decision_or_provider_imports() -> None:
    tree = ast.parse(CONTROL.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    forbidden = ("ai", "research", "provider", "JevDecision", "ResearchBrief")
    assert not any(
        term.casefold() in name.casefold() for name in imported for term in forbidden
    )
