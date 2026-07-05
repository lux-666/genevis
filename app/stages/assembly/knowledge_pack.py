"""Load knowledge packs for assembly parameter recommendation.

LOADER SELECTION GUIDE
======================
Two knowledge pack loaders exist with different error handling and use cases:

1. load_skill_context() [knowledge.py]
   - Use for: Interactive/routing contexts (e.g., route_selector)
   - Error handling: Graceful - returns empty dict if pack doesn't exist
   - Returns: {"rules", "skill_text", "cases", "allowed_actions"}
   - Cases format: Expects cases.yaml to be a list directly
   - Extra: Extracts allowed_actions from rules.allowed_actions

2. load_knowledge_pack() [THIS MODULE]
   - Use for: Strict validation contexts (e.g., parameter_recommender)
   - Error handling: Strict - raises FileNotFoundError on missing pack/files
   - Returns: {"rules", "cases", "skill"}
   - Cases format: accepts {"cases": [...]} or manifest {"case_files": [...]}
   - Rules format: accepts {"rules": [...]} or manifest {"rule_files": [...]}
   - Extra: None

Usage:
- Route selection, QC interpretation: Use load_skill_context()
- Parameter recommendation, prompt payloads: Use load_knowledge_pack()
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_PACK_ROOT = _PROJECT_ROOT / "descriptors" / "knowledge_packs" / "assembly"


def load_knowledge_pack(pack_id: str) -> dict[str, Any]:
    """Load rules.yaml, cases.yaml, SKILL.md for a knowledge pack.

    Strict loader: raises FileNotFoundError if pack or required files are missing.
    Use for contexts where complete knowledge pack is mandatory (e.g., parameter recommendation).
    See module docstring for when to use this vs load_skill_context().

    Args:
        pack_id: Knowledge pack identifier (e.g., "parameter_recommender")

    Returns:
        Dict with keys: "rules", "cases", "skill"

    Raises:
        FileNotFoundError: If knowledge pack directory or required files don't exist
        yaml.YAMLError: If YAML files are malformed
    """
    pack_dir = KNOWLEDGE_PACK_ROOT / pack_id
    if not pack_dir.exists():
        raise FileNotFoundError(f"Knowledge pack not found: {pack_dir}")

    rules_path = pack_dir / "rules.yaml"
    cases_path = pack_dir / "cases.yaml"
    skill_path = pack_dir / "SKILL.md"

    if not rules_path.exists():
        raise FileNotFoundError(f"rules.yaml not found in knowledge pack: {rules_path}")
    if not cases_path.exists():
        raise FileNotFoundError(f"cases.yaml not found in knowledge pack: {cases_path}")
    if not skill_path.exists():
        raise FileNotFoundError(f"SKILL.md not found in knowledge pack: {skill_path}")

    rules = _load_rules(pack_dir, rules_path)
    cases = _load_cases(pack_dir, cases_path)
    skill = skill_path.read_text(encoding="utf-8")

    return {
        "rules": rules,
        "cases": cases,
        "skill": skill,
    }


def select_evidence_cases(
    cases: list[dict[str, Any]],
    *,
    method_id: str,
    task: str,
    prompt_profile: dict[str, Any],
    min_cases: int = 3,
    max_cases: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select a small evidence subset for a parameter recommendation prompt.

    HARD FILTER: Cases with mismatched read_type are excluded before scoring.
    Only cases where case.features.read_type matches profile read_type are considered.
    This prevents cross-contamination between HiFi and ONT parameter recommendations.
    """
    profile_read_type = _lookup(prompt_profile, "assets.read_type")

    scored: list[tuple[int, str, dict[str, Any]]] = []
    for case in cases:
        # HARD FILTER: Exclude cases with mismatched read_type
        case_read_type = (case.get("features") or {}).get("read_type")
        if profile_read_type and case_read_type and case_read_type != profile_read_type:
            continue

        score = _case_score(case, method_id=method_id, task=task, prompt_profile=prompt_profile)
        if score > 0:
            scored.append((score, str(case.get("case_id") or ""), case))

    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = [case for _, _, case in scored[:max_cases]]
    warning = None
    if len(selected) < min_cases:
        warning = f"Only {len(selected)} compatible evidence cases selected; recommendations should stay conservative."

    return selected, {
        "selected_case_ids": [case.get("case_id") for case in selected],
        "selected_count": len(selected),
        "total_cases": len(cases),
        "warning": warning,
    }


def _load_cases(pack_dir: Path, manifest_path: Path) -> list[dict[str, Any]]:
    manifest = _load_yaml_mapping(manifest_path)
    cases: list[dict[str, Any]] = []
    cases.extend(_case_list(manifest))
    for rel_path in manifest.get("case_files") or []:
        case_path = _resolve_pack_path(pack_dir, rel_path)
        cases.extend(_case_list(_load_yaml_mapping(case_path)))
    return cases


def _load_rules(pack_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _load_yaml_mapping(manifest_path)
    combined = {key: value for key, value in manifest.items() if key not in {"rules", "rule_files"}}
    rules: list[dict[str, Any]] = []
    rules.extend(_rule_list(manifest))
    for rel_path in manifest.get("rule_files") or []:
        rule_path = _resolve_pack_path(pack_dir, rel_path)
        rules.extend(_rule_list(_load_yaml_mapping(rule_path)))
    combined["rules"] = rules
    return combined


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"cases": raw}
    raise TypeError(f"Expected YAML mapping or list in {path}")


def _resolve_pack_path(pack_dir: Path, rel_path: str) -> Path:
    path = pack_dir / rel_path
    if not path.exists():
        raise FileNotFoundError(f"Knowledge pack referenced file not found: {path}")
    return path


def _case_list(raw: dict[str, Any]) -> list[dict[str, Any]]:
    cases = raw.get("cases", [])
    return cases if isinstance(cases, list) else []


def _rule_list(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rules = raw.get("rules", [])
    return rules if isinstance(rules, list) else []


def _case_score(
    case: dict[str, Any],
    *,
    method_id: str,
    task: str,
    prompt_profile: dict[str, Any],
) -> int:
    features = case.get("features") or {}
    params = case.get("params") or {}
    case_method = params.get("tool")
    read_type = _lookup(prompt_profile, "assets.read_type")
    case_read_type = features.get("read_type")
    failure_mode = _lookup(prompt_profile, "previous_attempt.failure_class") or _lookup(
        prompt_profile, "assembly_review.failure_mode"
    )

    if task == "recommend_initial" and features.get("failure_mode"):
        return 0

    score = 0
    if case_method == method_id:
        score += 8
    elif _tool_family(case_method) == _tool_family(method_id):
        score += 3
    if read_type and case_read_type == read_type:
        score += 8
    if failure_mode and features.get("failure_mode") == failure_mode:
        score += 5

    sample_clade = _lookup(prompt_profile, "sample.clade")
    if sample_clade and features.get("clade") == sample_clade:
        score += 2

    coverage = _lookup(prompt_profile, "read_qc.effective_coverage_1kb") or _lookup(
        prompt_profile, "read_qc.effective_coverage_5kb"
    )
    if _same_bucket(coverage, features.get("coverage"), (30, 60)):
        score += 2

    genome_size = _lookup(prompt_profile, "genome_survey.estimated_genome_size")
    if _same_bucket(_bp_to_mb(genome_size), features.get("genome_size_mb"), (500, 3000, 10000)):
        score += 2

    heterozygosity = _lookup(prompt_profile, "genome_survey.heterozygosity")
    if _heterozygosity_bucket(heterozygosity) == features.get("heterozygosity"):
        score += 2

    read_n50_kb = _bp_to_kb(_lookup(prompt_profile, "read_qc.read_n50"))
    if _same_bucket(read_n50_kb, features.get("read_n50_kb"), (15, 30, 50)):
        score += 1

    return score


def _lookup(values: Any, path: str) -> Any:
    current = values
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _tool_family(method_id: Any) -> str | None:
    if not isinstance(method_id, str):
        return None
    return method_id.split("_", 1)[0]


def _same_bucket(left: Any, right: Any, thresholds: tuple[int, ...]) -> bool:
    if not _is_number(left) or not _is_number(right):
        return False
    return _bucket(float(left), thresholds) == _bucket(float(right), thresholds)


def _bucket(value: float, thresholds: tuple[int, ...]) -> int:
    for index, threshold in enumerate(thresholds):
        if value <= threshold:
            return index
    return len(thresholds)


def _heterozygosity_bucket(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.lower()
        return normalized if normalized in {"low", "medium", "high", "unknown"} else None
    if not _is_number(value):
        return None
    numeric = float(value)
    if numeric < 0.003:
        return "low"
    if numeric < 0.01:
        return "medium"
    return "high"


def _bp_to_mb(value: Any) -> float | None:
    return float(value) / 1_000_000 if _is_number(value) else None


def _bp_to_kb(value: Any) -> float | None:
    return float(value) / 1_000 if _is_number(value) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
