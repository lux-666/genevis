"""Assembly EvidenceCase validation and prompt-safe normalization."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from app.knowledge.evidence_cases import EvidenceCaseValidationError, validate_source_bundle

HIFIASM_EVIDENCE_SCHEMA_VERSION = 2
_ALLOWED_CASE_TYPES = {"initial", "retry"}
_ALLOWED_CURATION_STATUSES = {"draft", "approved", "rejected"}
_ALLOWED_EXPLICITNESS = {"explicit", "inferred"}


def is_hifiasm_evidence_case(case: dict[str, Any]) -> bool:
    """Return whether a case declares the current hifiasm evidence contract."""

    if case.get("schema_version") != HIFIASM_EVIDENCE_SCHEMA_VERSION:
        return False
    applicability = case.get("applicability") or {}
    intervention = case.get("intervention") or {}
    source_tool = applicability.get("source_tool") or intervention.get("tool")
    return _short_method_name(source_tool) == "hifiasm"


def validate_hifiasm_evidence_cases(
    cases: list[dict[str, Any]],
    *,
    pack_root: Path | None,
) -> list[dict[str, Any]]:
    """Validate hifiasm cases and attach only referenced exact excerpts.

    Legacy seed observations are intentionally ignored. Draft and rejected
    cases are retained for audit but remain ineligible for retrieval.
    """

    for case in cases:
        if not isinstance(case, dict) or case.get("schema_version") is None:
            continue
        applicability = case.get("applicability") or {}
        intervention = case.get("intervention") or {}
        source_tool = applicability.get("source_tool") or intervention.get("tool")
        if (
            _short_method_name(source_tool) == "hifiasm"
            and case.get("schema_version") != HIFIASM_EVIDENCE_SCHEMA_VERSION
        ):
            raise EvidenceCaseValidationError(
                f"{case.get('case_id') or '<missing-case-id>'}: unsupported hifiasm schema_version "
                f"{case.get('schema_version')!r}"
            )

    candidates = [
        case for case in cases if isinstance(case, dict) and is_hifiasm_evidence_case(case)
    ]
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
    if not isinstance(intervention, dict) or _short_method_name(intervention.get("tool")) != "hifiasm":
        raise EvidenceCaseValidationError(f"{case_id}: intervention.tool must be hifiasm")
    _validate_case_type_contract(case, intervention=intervention)

    result = deepcopy(case)
    result.pop("source_documents", None)
    result["source_excerpts"] = []
    result["retrieval_eligible"] = False
    if status != "approved":
        return result
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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
