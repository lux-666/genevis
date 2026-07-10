"""Assembly EvidenceCase validation and prompt-safe normalization."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from app.knowledge.evidence_cases import EvidenceCaseValidationError, validate_source_bundle

HIFIASM_EVIDENCE_SCHEMA_VERSION = 2
_ALLOWED_CASE_TYPES = {"initial", "retry"}
_ALLOWED_CURATION_STATUSES = {"draft", "approved", "rejected"}
_ALLOWED_EXPLICITNESS = {"explicit", "inferred"}
_NUMERIC_OBSERVED_FACTS = (
    "genome_size_mb",
    "coverage",
    "read_n50_kb",
    "heterozygosity",
    "ploidy",
)


def is_hifiasm_evidence_case(case: dict[str, Any]) -> bool:
    """Return whether a case must satisfy the current hifiasm contract."""

    return case.get("schema_version") == HIFIASM_EVIDENCE_SCHEMA_VERSION


def validate_hifiasm_evidence_cases(
    cases: list[dict[str, Any]],
    *,
    pack_root: Path | None,
) -> list[dict[str, Any]]:
    """Validate hifiasm cases and attach only referenced exact excerpts.

    Legacy seed observations are intentionally ignored. Draft and rejected
    cases are retained for audit but remain ineligible for retrieval.
    """

    candidates: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        if is_hifiasm_evidence_case(case):
            candidates.append(case)
            continue
        if _declares_hifiasm_contract(case):
            raise EvidenceCaseValidationError(
                f"{case.get('case_id') or '<missing-case-id>'}: unsupported hifiasm schema_version "
                f"{case.get('schema_version')!r}"
            )

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for case in candidates:
        case_id = str(case.get("case_id") or "")
        if not case_id:
            raise EvidenceCaseValidationError("hifiasm EvidenceCase requires case_id")
        if case_id in seen:
            raise EvidenceCaseValidationError(f"duplicate hifiasm EvidenceCase case_id: {case_id}")
        seen.add(case_id)
        normalized.append(_validate_case(case, pack_root=pack_root))
    return normalized


def _validate_case(case: dict[str, Any], *, pack_root: Path | None) -> dict[str, Any]:
    case_id = str(case["case_id"])
    case_type = case.get("case_type")
    if case_type not in _ALLOWED_CASE_TYPES:
        raise EvidenceCaseValidationError(f"{case_id}: case_type must be one of {sorted(_ALLOWED_CASE_TYPES)}")

    curation = case.get("curation")
    if not isinstance(curation, dict) or curation.get("status") not in _ALLOWED_CURATION_STATUSES:
        raise EvidenceCaseValidationError(
            f"{case_id}: curation.status must be one of {sorted(_ALLOWED_CURATION_STATUSES)}"
        )
    status = str(curation["status"])

    source = case.get("source")
    if not isinstance(source, dict) or source.get("kind") not in {"paper", "internal_run"}:
        raise EvidenceCaseValidationError(f"{case_id}: source.kind must be paper or internal_run")
    if source.get("kind") == "paper" and not any(source.get(key) for key in ("doi", "title")):
        raise EvidenceCaseValidationError(f"{case_id}: paper source requires doi or title")
    if source.get("kind") == "internal_run" and not source.get("internal_run_id"):
        raise EvidenceCaseValidationError(f"{case_id}: internal_run source requires internal_run_id")

    applicability = case.get("applicability")
    if not isinstance(applicability, dict):
        raise EvidenceCaseValidationError(f"{case_id}: applicability must be a mapping")
    if applicability.get("read_type") != "hifi":
        raise EvidenceCaseValidationError(f"{case_id}: applicability.read_type must be 'hifi'")
    if _short_method_name(applicability.get("source_tool")) != "hifiasm":
        raise EvidenceCaseValidationError(f"{case_id}: applicability.source_tool must be hifiasm")
    target_methods = applicability.get("target_methods")
    if not isinstance(target_methods, list) or not any(
        _short_method_name(method) == "hifiasm" for method in target_methods
    ):
        raise EvidenceCaseValidationError(f"{case_id}: applicability.target_methods must include hifiasm")

    intervention = case.get("intervention")
    if not isinstance(intervention, dict):
        raise EvidenceCaseValidationError(f"{case_id}: intervention must be a mapping")
    if _short_method_name(intervention.get("tool")) != "hifiasm":
        raise EvidenceCaseValidationError(f"{case_id}: intervention.tool must be hifiasm")
    _validate_case_type_contract(case, intervention=intervention)

    result = deepcopy(case)
    result.pop("source_documents", None)
    result["source_excerpts"] = []
    result["retrieval_eligible"] = False
    if status != "approved":
        return result
    _validate_approved_numeric_facts(case)
    if pack_root is None:
        raise EvidenceCaseValidationError(f"{case_id}: pack_root is required to validate approved sources")

    source_excerpts = validate_source_bundle(case, pack_root=pack_root, require_hashes=True)
    span_documents = {span["span_id"]: span["document"] for span in source_excerpts}
    referenced_span_ids = _validate_provenance(case, span_documents=span_documents)
    result["source_excerpts"] = [
        span for span in source_excerpts if span["span_id"] in referenced_span_ids
    ]
    result["retrieval_eligible"] = True
    return result


def _validate_case_type_contract(case: dict[str, Any], *, intervention: dict[str, Any]) -> None:
    case_id = str(case["case_id"])
    case_type = case["case_type"]
    previous_attempt = case.get("previous_attempt")
    parameter_changes = intervention.get("parameter_changes")

    if case_type == "initial":
        if previous_attempt not in (None, {}):
            raise EvidenceCaseValidationError(
                f"{case_id}: initial case must not define previous_attempt"
            )
        if parameter_changes not in (None, {}):
            raise EvidenceCaseValidationError(
                f"{case_id}: initial case must not define intervention.parameter_changes"
            )
        return

    if not isinstance(previous_attempt, dict):
        raise EvidenceCaseValidationError(f"{case_id}: retry case requires previous_attempt")
    if not isinstance(previous_attempt.get("parameters"), dict):
        raise EvidenceCaseValidationError(
            f"{case_id}: retry case requires previous_attempt.parameters mapping"
        )
    if not any(previous_attempt.get(key) for key in ("failure_mode", "execution_failure_class")):
        raise EvidenceCaseValidationError(
            f"{case_id}: retry case requires failure_mode or execution_failure_class"
        )
    if not isinstance(parameter_changes, dict) or not parameter_changes:
        raise EvidenceCaseValidationError(
            f"{case_id}: retry case requires non-empty intervention.parameter_changes"
        )
    for parameter_name, change in parameter_changes.items():
        field_name = f"intervention.parameter_changes.{parameter_name}"
        if not isinstance(change, dict):
            raise EvidenceCaseValidationError(f"{case_id}: {field_name} must be a mapping")
        missing = [key for key in ("from", "to") if key not in change]
        if missing:
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_name} requires {', '.join(missing)}"
            )


def _validate_provenance(case: dict[str, Any], *, span_documents: dict[str, str]) -> set[str]:
    case_id = str(case["case_id"])
    referenced: set[str] = set()

    for section_name in ("observed_facts", "outcomes"):
        section = case.get(section_name) or {}
        if not isinstance(section, dict):
            raise EvidenceCaseValidationError(f"{case_id}: {section_name} must be a mapping")
        for field_name, fact in section.items():
            if not isinstance(fact, dict):
                raise EvidenceCaseValidationError(f"{case_id}: {section_name}.{field_name} must be a mapping")
            field_path = f"{section_name}.{field_name}"
            span_ids = _span_ids(fact, case_id=case_id, field_name=field_path)
            if _has_numeric_evidence(fact) and not span_ids:
                raise EvidenceCaseValidationError(
                    f"{case_id}: numeric {field_path} requires evidence_span_ids"
                )
            _require_known_spans(case_id, field_path, span_ids, span_documents)
            referenced.update(span_ids)

    intervention = case.get("intervention") or {}
    parameters = intervention.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise EvidenceCaseValidationError(f"{case_id}: intervention.parameters must be a mapping")
    for parameter_name, parameter in parameters.items():
        if not isinstance(parameter, dict):
            raise EvidenceCaseValidationError(f"{case_id}: intervention.parameters.{parameter_name} must be a mapping")
        field_path = f"intervention.parameters.{parameter_name}"
        span_ids = _span_ids(parameter, case_id=case_id, field_name=field_path)
        if parameter.get("reported") is True and not span_ids:
            raise EvidenceCaseValidationError(
                f"{case_id}: reported parameter {parameter_name} requires evidence_span_ids"
            )
        _require_known_spans(case_id, field_path, span_ids, span_documents)
        if parameter.get("reported") is True:
            _require_document(case_id, parameter_name, span_ids, span_documents, "methods")
        referenced.update(span_ids)

    rationale_claims = case.get("rationale_claims") or []
    if not isinstance(rationale_claims, list):
        raise EvidenceCaseValidationError(f"{case_id}: rationale_claims must be a list")
    for index, claim in enumerate(rationale_claims):
        if not isinstance(claim, dict):
            raise EvidenceCaseValidationError(f"{case_id}: rationale_claims[{index}] must be a mapping")
        explicitness = claim.get("explicitness")
        if explicitness not in _ALLOWED_EXPLICITNESS:
            raise EvidenceCaseValidationError(
                f"{case_id}: rationale_claims[{index}].explicitness must be explicit or inferred"
            )
        field_path = f"rationale_claims[{index}]"
        span_ids = _span_ids(claim, case_id=case_id, field_name=field_path)
        if explicitness == "explicit" and not span_ids:
            raise EvidenceCaseValidationError(
                f"{case_id}: explicit rationale_claims[{index}] requires evidence_span_ids"
            )
        _require_known_spans(case_id, f"rationale_claims[{index}]", span_ids, span_documents)
        if explicitness == "explicit":
            _require_document(case_id, f"rationale_claims[{index}]", span_ids, span_documents, "methods")
        referenced.update(span_ids)
    return referenced


def _span_ids(value: dict[str, Any], *, case_id: str, field_name: str) -> list[str]:
    raw = value.get("evidence_span_ids") or []
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_name}.evidence_span_ids must be a list of non-empty strings"
        )
    return raw


def _require_known_spans(
    case_id: str,
    field_name: str,
    span_ids: Iterable[str],
    span_documents: dict[str, str],
) -> None:
    for span_id in span_ids:
        if span_id not in span_documents:
            raise EvidenceCaseValidationError(f"{case_id}: {field_name} references unknown span_id {span_id!r}")


def _require_document(
    case_id: str,
    field_name: str,
    span_ids: Iterable[str],
    span_documents: dict[str, str],
    expected_document: str,
) -> None:
    wrong = [span_id for span_id in span_ids if span_documents.get(span_id) != expected_document]
    if wrong:
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_name} must cite {expected_document} spans, got {wrong}"
        )


def _short_method_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.rsplit(".", 1)[-1]


def _declares_hifiasm_contract(case: dict[str, Any]) -> bool:
    applicability = case.get("applicability")
    if isinstance(applicability, dict):
        if _short_method_name(applicability.get("source_tool")) == "hifiasm":
            return True
        target_methods = applicability.get("target_methods")
        if isinstance(target_methods, list) and any(
            _short_method_name(method) == "hifiasm" for method in target_methods
        ):
            return True
    intervention = case.get("intervention")
    return bool(
        isinstance(intervention, dict)
        and _short_method_name(intervention.get("tool")) == "hifiasm"
    )


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _validate_approved_numeric_facts(case: dict[str, Any]) -> None:
    case_id = str(case["case_id"])
    observed_facts = case.get("observed_facts")
    if observed_facts is None:
        observed_facts = {}
    if not isinstance(observed_facts, dict):
        raise EvidenceCaseValidationError(f"{case_id}: observed_facts must be a mapping")
    for field_name in _NUMERIC_OBSERVED_FACTS:
        if field_name not in observed_facts:
            continue
        fact = observed_facts[field_name]
        if not isinstance(fact, dict):
            raise EvidenceCaseValidationError(
                f"{case_id}: observed_facts.{field_name} must be a mapping"
            )
        _validate_numeric_fact(
            fact,
            case_id=case_id,
            field_path=f"observed_facts.{field_name}",
            field_name=field_name,
        )

    outcomes = case.get("outcomes")
    if outcomes is None:
        outcomes = {}
    if not isinstance(outcomes, dict):
        raise EvidenceCaseValidationError(f"{case_id}: outcomes must be a mapping")
    for field_name, fact in outcomes.items():
        if not isinstance(fact, dict):
            raise EvidenceCaseValidationError(f"{case_id}: outcomes.{field_name} must be a mapping")
        _validate_numeric_fact(
            fact,
            case_id=case_id,
            field_path=f"outcomes.{field_name}",
            field_name=field_name,
        )


def _validate_numeric_fact(
    fact: dict[str, Any],
    *,
    case_id: str,
    field_path: str,
    field_name: str,
) -> None:
    value = fact.get("value")
    uncertainty = fact.get("uncertainty")
    interval: tuple[float, float] | None = None

    if uncertainty is not None:
        if not isinstance(uncertainty, dict):
            raise EvidenceCaseValidationError(f"{case_id}: {field_path}.uncertainty must be a mapping")
        kind = uncertainty.get("kind")
        if kind not in {"point", "interval"}:
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path}.uncertainty.kind must be point or interval"
            )
        if kind == "point" and any(bound in uncertainty for bound in ("min", "max")):
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path}.uncertainty point must not define min or max"
            )
        if kind == "interval":
            if field_name == "ploidy":
                raise EvidenceCaseValidationError(
                    f"{case_id}: {field_path} does not support interval uncertainty"
                )
            missing = [bound for bound in ("min", "max") if bound not in uncertainty]
            if missing:
                raise EvidenceCaseValidationError(
                    f"{case_id}: {field_path}.uncertainty interval requires {', '.join(missing)}"
                )
            lower = uncertainty.get("min")
            upper = uncertainty.get("max")
            if not _is_number(lower) or not _is_number(upper):
                raise EvidenceCaseValidationError(
                    f"{case_id}: {field_path}.uncertainty min and max must be finite numbers"
                )
            if lower > upper:
                raise EvidenceCaseValidationError(
                    f"{case_id}: {field_path}.uncertainty min must be <= max"
                )
            interval = (float(lower), float(upper))

    if value is None and interval is None:
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path} requires a finite numeric value or interval"
        )
    if value is not None and not _is_number(value):
        raise EvidenceCaseValidationError(f"{case_id}: {field_path}.value must be a finite number")
    if value is not None and interval is not None and not interval[0] <= float(value) <= interval[1]:
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path}.value must fall within its uncertainty interval"
        )

    values = [float(value)] if value is not None else []
    if interval is not None:
        values.extend(interval)
    if field_name in {"genome_size_mb", "coverage", "read_n50_kb"} and any(
        item <= 0 for item in values
    ):
        raise EvidenceCaseValidationError(f"{case_id}: {field_path} values must be positive")
    if field_name == "heterozygosity" and any(not 0 <= item <= 1 for item in values):
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path} values must be fractions between 0 and 1"
        )
    if field_name == "ploidy" and any(item <= 0 or not item.is_integer() for item in values):
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path}.value must be a positive integer"
        )


def _has_numeric_evidence(fact: dict[str, Any]) -> bool:
    if _is_number(fact.get("value")):
        return True
    uncertainty = fact.get("uncertainty")
    return bool(
        isinstance(uncertainty, dict)
        and uncertainty.get("kind") == "interval"
        and _is_number(uncertainty.get("min"))
        and _is_number(uncertainty.get("max"))
    )
