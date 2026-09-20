"""Shared knowledge pack loading utilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KNOWLEDGE_ROOT = _PROJECT_ROOT / "descriptors" / "knowledge_packs"


def load_knowledge_pack(
    pack_id: str,
    *,
    domain: str = "assembly",
    root: Path | None = None,
    strict: bool = True,
    include_actions: bool = False,
    required_files: tuple[str, ...] = ("rules", "skill"),
) -> dict[str, Any]:
    """Load a knowledge pack with shared strict/graceful behavior."""

    pack_root = root or (DEFAULT_KNOWLEDGE_ROOT / domain)
    pack_dir = pack_root / pack_id
    if not pack_dir.exists():
        if strict:
            raise FileNotFoundError(f"Knowledge pack not found: {pack_dir}")
        return _empty_pack()

    paths = {
        "rules": pack_dir / "rules.yaml",
        "skill": pack_dir / "SKILL.md",
        "strategies": pack_dir / "strategies.yaml",
    }
    for file_key in required_files:
        path = paths[file_key]
        if not path.exists():
            if strict:
                raise FileNotFoundError(
                    f"{path.name} not found in knowledge pack: {path}"
                )
            return _empty_pack()

    rules = _load_rules(pack_dir, paths["rules"]) if paths["rules"].exists() else {}
    skill = (
        paths["skill"].read_text(encoding="utf-8") if paths["skill"].exists() else None
    )
    strategies = (
        _load_yaml_mapping(paths["strategies"]) if paths["strategies"].exists() else {}
    )
    allowed_actions = list(rules.get("allowed_actions", [])) if include_actions else []

    return {
        "rules": rules,
        "skill": skill,
        "skill_text": skill,
        "strategies": strategies,
        "allowed_actions": allowed_actions,
        "pack_root": str(pack_dir.resolve()),
    }


def _empty_pack() -> dict[str, Any]:
    return {
        "rules": {},
        "skill": None,
        "skill_text": None,
        "strategies": {},
        "allowed_actions": [],
    }


def _load_rules(pack_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _load_yaml_mapping(manifest_path)
    combined = {
        key: value
        for key, value in manifest.items()
        if key not in {"rules", "rule_files"}
    }
    rules: list[dict[str, Any]] = []
    rules.extend(_rule_list(manifest))
    for rel_path in manifest.get("rule_files") or []:
        rule_path = _resolve_pack_path(pack_dir, rel_path)
        rules.extend(_rule_list(_load_yaml_mapping(rule_path)))
    if rules or "rules" in manifest or "rule_files" in manifest:
        combined["rules"] = rules
    return combined


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict):
        return raw
    raise TypeError(f"Expected YAML mapping in {path}")


def _resolve_pack_path(pack_dir: Path, rel_path: str) -> Path:
    relative = Path(rel_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Knowledge pack path escapes pack root: {rel_path}")
    root = pack_dir.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Knowledge pack path escapes pack root: {rel_path}")
    if not path.exists():
        raise FileNotFoundError(f"Knowledge pack referenced file not found: {path}")
    return path


def _rule_list(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rules = raw.get("rules", [])
    return rules if isinstance(rules, list) else []
