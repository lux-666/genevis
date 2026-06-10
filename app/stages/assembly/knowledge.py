"""Assembly skill-like knowledge pack loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PACK_ROOT = Path(__file__).resolve().parents[3] / "descriptors" / "knowledge_packs" / "assembly"


def load_skill_context(pack_name: str) -> dict[str, Any]:
    """Load rules, skill text, cases, and allowed actions for an assembly pack."""
    pack_dir = _PACK_ROOT / pack_name
    if not pack_dir.exists():
        return {"rules": {}, "skill_text": None, "cases": [], "allowed_actions": []}

    rules: dict[str, Any] = {}
    rules_path = pack_dir / "rules.yaml"
    if rules_path.exists():
        rules = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}

    skill_text: str | None = None
    skill_path = pack_dir / "SKILL.md"
    if skill_path.exists():
        skill_text = skill_path.read_text(encoding="utf-8")

    cases: list[dict[str, Any]] = []
    cases_path = pack_dir / "cases.yaml"
    if cases_path.exists():
        cases = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or []

    return {
        "rules": rules,
        "skill_text": skill_text,
        "cases": cases,
        "allowed_actions": list(rules.get("allowed_actions", [])),
    }
