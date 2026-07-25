"""Assembly EvidenceCase validation and prompt-safe normalization."""

from __future__ import annotations

import json
import math
import weakref
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Protocol, TypeGuard

import yaml

from app.knowledge.evidence_cases import (
    MAX_SOURCE_EXCERPT_CHARS,
    MAX_SOURCE_EXCERPTS_PER_CASE,
    MAX_TOTAL_SOURCE_EXCERPT_CHARS_PER_CASE,
    EvidenceCaseValidationError,
    validate_methods_source,
    validate_methods_source_shape,
    validate_referenced_excerpt_budget,
)
from app.knowledge.packs import ASSEMBLY_EVIDENCE_CASE_SCHEMA

HIFIASM_EVIDENCE_SCHEMA_VERSION = 3
MAX_EVIDENCE_TEXT_FIELD_CHARS = 1_000
MAX_PROMPT_SAFE_TEXT_CHARS_PER_CASE = 16_000
_EVIDENCE_MANIFEST_VERSION = 2

_ALLOWED_CASE_TYPES = {"initial"}
_ALLOWED_CURATION_STATUSES = {"draft", "approved", "rejected"}
_ALLOWED_EXPLICITNESS = {"explicit", "inferred"}
_MAX_DYNAMIC_MAPPING_ITEMS = 64
_MAX_RATIONALE_CLAIMS = 32
_MAX_SHORT_STRING_LIST_ITEMS = 16
_JSON_SCALAR = object()
_TEXT = object()
_OPTIONAL_TEXT = object()
_SCHEMA_VERSION = object()
_SOURCE_EXCERPT_TEXT = object()

_NUMERIC_OBSERVED_FACTS = (
    "genome_size_mb",
    "coverage",
    "read_n50_kb",
    "heterozygosity",
    "ploidy",
)

_FACT_SCHEMA = {
    "value": _JSON_SCALAR,
    "unit": _OPTIONAL_TEXT,
    "uncertainty": {"kind": _TEXT, "min": _JSON_SCALAR, "max": _JSON_SCALAR},
    "evidence_span_ids": ("strings", MAX_SOURCE_EXCERPTS_PER_CASE),
}
_PARAMETER_SCHEMA = {
    "value": _JSON_SCALAR,
    "reported": _JSON_SCALAR,
    "evidence_span_ids": ("strings", MAX_SOURCE_EXCERPTS_PER_CASE),
}
_RATIONALE_SCHEMA = {
    "claim_id": _TEXT,
    "statement": _TEXT,
    "explicitness": _TEXT,
    "effect_area": _TEXT,
    "evidence_span_ids": ("strings", MAX_SOURCE_EXCERPTS_PER_CASE),
}
_CASE_SCHEMA = {
    "schema_version": _SCHEMA_VERSION,
    "case_id": _TEXT,
    "case_type": _TEXT,
    "source": {
        "kind": _TEXT,
        "title": _OPTIONAL_TEXT,
        "doi": _OPTIONAL_TEXT,
        "year": _JSON_SCALAR,
        "artifact_refs": ("strings", _MAX_SHORT_STRING_LIST_ITEMS),
    },
    "applicability": {
        "read_type": _TEXT,
        "source_tool": _TEXT,
        "source_tool_version": _OPTIONAL_TEXT,
        "target_methods": ("strings", _MAX_SHORT_STRING_LIST_ITEMS),
        "assembly_goal": _OPTIONAL_TEXT,
    },
    "observed_facts": {
        field_name: _FACT_SCHEMA
        for field_name in (
            "organism",
            "clade",
            *_NUMERIC_OBSERVED_FACTS,
        )
    },
    "intervention": {
        "tool": _TEXT,
        "version": _OPTIONAL_TEXT,
        "parameters": ("mapping", _PARAMETER_SCHEMA, _MAX_DYNAMIC_MAPPING_ITEMS),
    },
    "rationale_claims": ("list", _RATIONALE_SCHEMA, _MAX_RATIONALE_CLAIMS),
    "outcomes": {
        field_name: _FACT_SCHEMA
        for field_name in (
            "assembly_size_mb",
            "contig_n50_mb",
            "busco_complete",
            "busco_duplicate",
        )
    },
    "curation": {
        "status": _TEXT,
        "curator": _OPTIONAL_TEXT,
        "reviewed_at": _OPTIONAL_TEXT,
        "notes": _OPTIONAL_TEXT,
    },
}
_EVIDENCE_SPAN_SCHEMA = {
    "span_id": _TEXT,
    "quote": _SOURCE_EXCERPT_TEXT,
}
_SOURCE_EXCERPT_SCHEMA = {
    "span_id": _TEXT,
    "document": _TEXT,
    "quote": _SOURCE_EXCERPT_TEXT,
}
_CASE_SCHEMA["methods_path"] = _TEXT
_CASE_SCHEMA["evidence_spans"] = (
    "list",
    _EVIDENCE_SPAN_SCHEMA,
    MAX_SOURCE_EXCERPTS_PER_CASE,
)
_PROMPT_CASE_SCHEMA = {
    field_name: field_schema
    for field_name, field_schema in _CASE_SCHEMA.items()
    if field_name not in {"curation", "methods_path", "evidence_spans"}
}


def is_hifiasm_evidence_case(case: dict[str, Any]) -> bool:
    """Return whether a case must satisfy the current hifiasm contract."""

    return (
        type(case.get("schema_version")) is int
        and case["schema_version"] == HIFIASM_EVIDENCE_SCHEMA_VERSION
    )


def validate_hifiasm_evidence_cases(
    cases: list[dict[str, Any]],
    *,
    pack_root: Path | None,
) -> list[dict[str, Any]]:
    """Validate hifiasm cases and attach only referenced declared excerpts.

    Legacy seed observations are intentionally ignored. Draft and rejected
    cases are retained for audit but remain ineligible for retrieval.
    """

    candidates = _manifest_hifiasm_cases(pack_root) if pack_root is not None else None
    if candidates is None:
        candidates = []
        for case in cases:
            if not isinstance(case, dict) or "schema_version" not in case:
                continue
            if is_hifiasm_evidence_case(case):
                candidates.append(case)
                continue
            raise EvidenceCaseValidationError(
                f"{case.get('case_id') or '<missing-case-id>'}: unsupported hifiasm "
                "schema_version "
                f"{case.get('schema_version')!r}"
            )
    else:
        for case in candidates:
            if not is_hifiasm_evidence_case(case):
                raise EvidenceCaseValidationError(
                    f"{case.get('case_id') or '<missing-case-id>'}: hifiasm v3 shard "
                    "requires "
                    f"schema_version=3, got {case.get('schema_version')!r}"
                )

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for case in candidates:
        case_id = str(case.get("case_id") or "")
        if not case_id:
            raise EvidenceCaseValidationError("hifiasm EvidenceCase requires case_id")
        if case_id in seen:
            raise EvidenceCaseValidationError(
                f"duplicate hifiasm EvidenceCase case_id: {case_id}"
            )
        seen.add(case_id)
        normalized.append(_validate_case(case, pack_root=pack_root))
    return normalized


class _ValidateCase(Protocol):
    def __call__(
        self,
        case: dict[str, Any],
        *,
        pack_root: Path | None,
    ) -> dict[str, Any]: ...


class _CaseProjector(Protocol):
    def __call__(self, case: dict[str, Any]) -> dict[str, Any]: ...


class _RankCase(Protocol):
    def __call__(
        self,
        case: dict[str, Any],
        *,
        numeric_match: dict[str, Any],
    ) -> dict[str, Any]: ...


def _build_validation_boundary() -> tuple[
    _ValidateCase,
    _CaseProjector,
    _CaseProjector,
    _RankCase,
    _CaseProjector,
]:
    """Track validated case semantics outside caller-writable mapping state."""

    registry: dict[
        int,
        tuple[weakref.ReferenceType[Any], tuple[str, str, str | None, bool]],
    ] = {}

    class ValidatedCase(dict[str, Any]):
        __slots__ = ("__weakref__",)

        def __new__(cls, *args: Any, **kwargs: Any) -> "ValidatedCase":
            raise TypeError(
                "validated hifiasm cases can only be created by Task 2 validation"
            )

        def __copy__(self) -> "ValidatedCase":
            return _copy_registered(self)

        def __deepcopy__(self, memo: dict[int, Any]) -> "ValidatedCase":
            return _copy_registered(self, memo=memo)

    def _semantic_proof(case: dict[str, Any]) -> str:
        return json.dumps(case, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _registered_proof(
        case: dict[str, Any],
    ) -> tuple[str, str, str | None, bool] | None:
        entry = registry.get(id(case))
        if entry is None or entry[0]() is not case:
            return None
        return entry[1]

    def _register(
        case: ValidatedCase,
        proof: tuple[str, str, str | None, bool],
    ) -> ValidatedCase:
        case_id = id(case)

        def _discard(reference: weakref.ReferenceType[Any]) -> None:
            entry = registry.get(case_id)
            if entry is not None and entry[0] is reference:
                registry.pop(case_id, None)

        registry[case_id] = (weakref.ref(case, _discard), proof)
        return case

    def _new_registered(
        values: dict[str, Any],
        *,
        kind: str,
        proof: tuple[str, str, str | None, bool] | None = None,
        curation_status: str | None = None,
        retrieval_eligible: bool = False,
    ) -> ValidatedCase:
        case = dict.__new__(ValidatedCase)
        dict.__init__(case, values)
        return _register(
            case,
            proof or (kind, _semantic_proof(case), curation_status, retrieval_eligible),
        )

    def _require_registered(
        case: dict[str, Any],
        *,
        expected_kind: str | None = None,
        require_retrieval_eligible: bool = False,
    ) -> tuple[str, str, str | None, bool]:
        case_id = str(case.get("case_id") or "<missing-case-id>")
        proof = _registered_proof(case)
        if proof is None or (expected_kind is not None and proof[0] != expected_kind):
            raise EvidenceCaseValidationError(
                f"{case_id}: case requires Task 2 validation-owned attestation"
            )
        if _semantic_proof(case) != proof[1]:
            raise EvidenceCaseValidationError(
                f"{case_id}: case differs from its Task 2 validation-owned attestation"
            )
        if require_retrieval_eligible and (
            proof[2] != "approved" or proof[3] is not True
        ):
            raise EvidenceCaseValidationError(
                f"{case_id}: Task 2 attestation is not retrieval-eligible"
            )
        return proof

    def _copy_registered(
        case: ValidatedCase,
        *,
        memo: dict[int, Any] | None = None,
    ) -> ValidatedCase:
        proof = _require_registered(case)
        copied = dict.__new__(ValidatedCase)
        if memo is None:
            values = dict(case)
        else:
            memo[id(case)] = copied
            values = deepcopy(dict(case), memo)
        dict.__init__(copied, values)
        return _register(copied, proof)

    def _validated_case(values: dict[str, Any]) -> ValidatedCase:
        curation = values.get("curation") or {}
        return _new_registered(
            values,
            kind="validated",
            curation_status=curation.get("status"),
            retrieval_eligible=values.get("retrieval_eligible") is True,
        )

    def _validate_case(
        case: dict[str, Any], *, pack_root: Path | None
    ) -> dict[str, Any]:
        case_id = str(case.get("case_id") or "<missing-case-id>")
        _reject_unknown_fields(
            case,
            allowed=set(_CASE_SCHEMA),
            case_id=case_id,
            field_path="case",
        )
        validate_methods_source_shape(case, required=False)
        result = _normalize_case_fields(case, schema=_CASE_SCHEMA, case_id=case_id)
        case_id_value = result.get("case_id")
        if not isinstance(case_id_value, str) or not case_id_value.strip():
            raise EvidenceCaseValidationError("hifiasm EvidenceCase requires case_id")
        case_id = case_id_value

        case_type = result.get("case_type")
        if case_type not in _ALLOWED_CASE_TYPES:
            raise EvidenceCaseValidationError(
                f"{case_id}: case_type must be one of {sorted(_ALLOWED_CASE_TYPES)}"
            )

        curation = result.get("curation")
        if (
            not isinstance(curation, dict)
            or curation.get("status") not in _ALLOWED_CURATION_STATUSES
        ):
            raise EvidenceCaseValidationError(
                f"{case_id}: curation.status must be one of "
                f"{sorted(_ALLOWED_CURATION_STATUSES)}"
            )
        status = str(curation["status"])
        if status == "approved" and not curation.get("curator"):
            raise EvidenceCaseValidationError(
                f"{case_id}: approved case requires curation.curator"
            )

        source = result.get("source")
        if not isinstance(source, dict) or source.get("kind") != "paper":
            raise EvidenceCaseValidationError(f"{case_id}: source.kind must be paper")
        if not any(source.get(key) for key in ("doi", "title")):
            raise EvidenceCaseValidationError(
                f"{case_id}: paper source requires doi or title"
            )
        year = source.get("year")
        if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
            raise EvidenceCaseValidationError(
                f"{case_id}: source.year must be an integer"
            )

        applicability = result.get("applicability")
        if not isinstance(applicability, dict):
            raise EvidenceCaseValidationError(
                f"{case_id}: applicability must be a mapping"
            )
        for field_name in ("read_type", "source_tool"):
            if not isinstance(applicability.get(field_name), str):
                raise EvidenceCaseValidationError(
                    f"{case_id}: applicability.{field_name} is required"
                )

        intervention = result.get("intervention")
        if not isinstance(intervention, dict):
            raise EvidenceCaseValidationError(
                f"{case_id}: intervention must be a mapping"
            )
        if not isinstance(intervention.get("tool"), str):
            raise EvidenceCaseValidationError(
                f"{case_id}: intervention.tool is required"
            )
        _validate_parameter_flags(intervention, case_id=case_id)
        _validate_case_type_contract(result)

        result["source_excerpts"] = []
        result["retrieval_eligible"] = False
        if status != "approved":
            return _validated_case(result)

        _validate_approved_numeric_facts(result)
        if pack_root is None:
            raise EvidenceCaseValidationError(
                f"{case_id}: pack_root is required to validate approved sources"
            )

        source_excerpts = validate_methods_source(case, pack_root=pack_root)
        span_documents = {span["span_id"]: span["document"] for span in source_excerpts}
        referenced_span_ids = _validate_provenance(
            result, span_documents=span_documents
        )
        validate_referenced_excerpt_budget(
            source_excerpts,
            referenced_span_ids=referenced_span_ids,
            case_id=case_id,
        )
        result["source_excerpts"] = [
            {key: span[key] for key in ("span_id", "document", "quote")}
            for span in source_excerpts
            if span["span_id"] in referenced_span_ids
        ]
        result["retrieval_eligible"] = True
        validated = _validated_case(result)
        project_hifiasm_prompt_safe_case(validated)
        return validated

    def project_hifiasm_retrieval_case(case: dict[str, Any]) -> dict[str, Any]:
        """Return a stable full case snapshot only after Task 2 attestation checks."""

        _require_registered(case, expected_kind="validated")
        return deepcopy(case)

    def _project_prompt_fields(case: dict[str, Any]) -> dict[str, Any]:
        case_id = str(case.get("case_id") or "<missing-case-id>")
        _reject_unknown_fields(
            case,
            allowed=set(_CASE_SCHEMA) | {"source_excerpts", "retrieval_eligible"},
            case_id=case_id,
            field_path="case",
        )
        validate_methods_source_shape(case, required=False)
        normalized = _normalize_case_fields(
            case, schema=_PROMPT_CASE_SCHEMA, case_id=case_id
        )
        if "source_excerpts" in case:
            source_excerpts = _normalize_value(
                case["source_excerpts"],
                ("list", _SOURCE_EXCERPT_SCHEMA, MAX_SOURCE_EXCERPTS_PER_CASE),
                case_id=case_id,
                field_path="source_excerpts",
            )
            _validate_unique_span_ids(source_excerpts, case_id=case_id)
            normalized["source_excerpts"] = source_excerpts
        _validate_prompt_safe_text_budget(normalized, case_id=case_id)
        return normalized

    def project_hifiasm_prompt_safe_case(case: dict[str, Any]) -> dict[str, Any]:
        """Project only Task 2-owned prompt fields from a validated case.

        This public boundary deliberately cannot mint or replace Task 3 ranking
        metadata.  Its output is a Task 2 prompt projection, not a ranked case,
        and therefore cannot be consumed directly by Task 4.
        """

        proof = _require_registered(
            case,
            expected_kind="validated",
            require_retrieval_eligible=True,
        )
        return _new_registered(
            _project_prompt_fields(case),
            kind="prompt_base",
            curation_status=proof[2],
            retrieval_eligible=proof[3],
        )

    def _rank_hifiasm_prompt_case(
        case: dict[str, Any],
        *,
        numeric_match: dict[str, Any],
    ) -> dict[str, Any]:
        """Perform the one-way Task 2 validated -> Task 3 ranked transition."""

        proof = _require_registered(
            case,
            expected_kind="validated",
            require_retrieval_eligible=True,
        )
        if not isinstance(numeric_match, dict):
            raise EvidenceCaseValidationError("numeric_match must be a mapping")
        normalized = _project_prompt_fields(case)
        normalized["numeric_match"] = deepcopy(numeric_match)
        return _new_registered(
            normalized,
            kind="ranked",
            curation_status=proof[2],
            retrieval_eligible=proof[3],
        )

    def _project_hifiasm_ranked_prompt_case(case: dict[str, Any]) -> dict[str, Any]:
        """Return a copy only when the case carries Task 3-owned ranked proof."""

        _require_registered(
            case,
            expected_kind="ranked",
            require_retrieval_eligible=True,
        )
        return deepcopy(case)

    return (
        _validate_case,
        project_hifiasm_retrieval_case,
        project_hifiasm_prompt_safe_case,
        _rank_hifiasm_prompt_case,
        _project_hifiasm_ranked_prompt_case,
    )


(
    _validate_case,
    project_hifiasm_retrieval_case,
    project_hifiasm_prompt_safe_case,
    _rank_hifiasm_prompt_case,
    _project_hifiasm_ranked_prompt_case,
) = _build_validation_boundary()
del _build_validation_boundary


def _normalize_case_fields(
    case: dict[str, Any],
    *,
    schema: dict[str, Any],
    case_id: str,
) -> dict[str, Any]:
    return {
        field_name: _normalize_value(
            case[field_name],
            field_schema,
            case_id=case_id,
            field_path=field_name,
        )
        for field_name, field_schema in schema.items()
        if field_name in case
    }


def _validate_unique_span_ids(spans: list[dict[str, Any]], *, case_id: str) -> None:
    seen: set[str] = set()
    for span in spans:
        span_id = span["span_id"]
        if span_id in seen:
            raise EvidenceCaseValidationError(
                f"{case_id}: duplicate source_excerpts span_id {span_id!r}"
            )
        seen.add(span_id)


def _normalize_value(value: Any, schema: Any, *, case_id: str, field_path: str) -> Any:
    if schema is _JSON_SCALAR:
        _validate_prompt_scalar(value, case_id=case_id, field_path=field_path)
        return value
    if schema is _TEXT:
        _validate_bounded_text(value, case_id=case_id, field_path=field_path)
        return value
    if schema is _OPTIONAL_TEXT:
        if value is not None:
            _validate_bounded_text(value, case_id=case_id, field_path=field_path)
        return value
    if schema is _SCHEMA_VERSION:
        if type(value) is not int or value != HIFIASM_EVIDENCE_SCHEMA_VERSION:
            raise EvidenceCaseValidationError(
                f"{case_id}: schema_version must be the integer "
                f"{HIFIASM_EVIDENCE_SCHEMA_VERSION}"
            )
        return value
    if schema is _SOURCE_EXCERPT_TEXT:
        _validate_bounded_text(
            value,
            case_id=case_id,
            field_path=field_path,
            max_chars=MAX_SOURCE_EXCERPT_CHARS,
        )
        return value

    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path} must be a mapping"
            )
        _reject_unknown_fields(
            value, allowed=set(schema), case_id=case_id, field_path=field_path
        )
        return {
            field_name: _normalize_value(
                value[field_name],
                field_schema,
                case_id=case_id,
                field_path=f"{field_path}.{field_name}",
            )
            for field_name, field_schema in schema.items()
            if field_name in value
        }

    kind = schema[0]
    if kind == "strings":
        return _normalize_string_list(
            value,
            max_items=schema[1],
            case_id=case_id,
            field_path=field_path,
        )
    if kind == "list":
        if not isinstance(value, list):
            raise EvidenceCaseValidationError(f"{case_id}: {field_path} must be a list")
        max_items = schema[2]
        if len(value) > max_items:
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path} may contain at most {max_items} entries"
            )
        return [
            _normalize_value(
                item,
                schema[1],
                case_id=case_id,
                field_path=f"{field_path}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    if kind == "mapping":
        if not isinstance(value, dict):
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path} must be a mapping"
            )
        max_items = schema[2]
        if len(value) > max_items:
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path} may contain at most {max_items} entries"
            )
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            _validate_bounded_text(key, case_id=case_id, field_path=f"{field_path} key")
            normalized[key] = _normalize_value(
                item,
                schema[1],
                case_id=case_id,
                field_path=f"{field_path}.{key}",
            )
        return normalized
    raise AssertionError(f"unknown evidence schema kind: {kind}")


def _normalize_string_list(
    value: Any,
    *,
    max_items: int,
    case_id: str,
    field_path: str,
) -> list[str]:
    if not isinstance(value, list):
        raise EvidenceCaseValidationError(f"{case_id}: {field_path} must be a list")
    if len(value) > max_items:
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path} may contain at most {max_items} entries"
        )
    normalized: list[str] = []
    for index, item in enumerate(value):
        _validate_bounded_text(
            item, case_id=case_id, field_path=f"{field_path}[{index}]"
        )
        if item in normalized:
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path} must not contain duplicates"
            )
        normalized.append(item)
    return normalized


def _validate_prompt_scalar(value: Any, *, case_id: str, field_path: str) -> None:
    if value is None or isinstance(value, bool) or _is_number(value):
        return
    if isinstance(value, str):
        _validate_bounded_text(value, case_id=case_id, field_path=field_path)
        return
    raise EvidenceCaseValidationError(
        f"{case_id}: {field_path} must be a finite number, boolean, string, or null"
    )


def _validate_bounded_text(
    value: Any,
    *,
    case_id: str,
    field_path: str,
    max_chars: int = MAX_EVIDENCE_TEXT_FIELD_CHARS,
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path} must be a non-empty string"
        )
    if len(value) > max_chars:
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path} exceeds the {max_chars}-character limit"
        )


def _validate_prompt_safe_text_budget(value: dict[str, Any], *, case_id: str) -> None:
    structured_value = {
        key: item for key, item in value.items() if key != "source_excerpts"
    }
    structured_total = _text_character_count(structured_value)
    if structured_total > MAX_PROMPT_SAFE_TEXT_CHARS_PER_CASE:
        raise EvidenceCaseValidationError(
            f"{case_id}: prompt-safe case text exceeds the "
            f"{MAX_PROMPT_SAFE_TEXT_CHARS_PER_CASE}-character limit"
        )
    excerpt_chars = sum(
        len(span["quote"]) for span in value.get("source_excerpts") or []
    )
    if excerpt_chars > MAX_TOTAL_SOURCE_EXCERPT_CHARS_PER_CASE:
        raise EvidenceCaseValidationError(
            f"{case_id}: prompt-safe source_excerpts exceed the "
            f"{MAX_TOTAL_SOURCE_EXCERPT_CHARS_PER_CASE}-character limit"
        )


def _text_character_count(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_character_count(item) for item in value)
    if isinstance(value, dict):
        return sum(
            len(str(key)) + _text_character_count(item) for key, item in value.items()
        )
    return 0


def _reject_unknown_fields(
    value: dict[Any, Any],
    *,
    allowed: set[str],
    case_id: str,
    field_path: str,
) -> None:
    unknown = [key for key in value if key not in allowed]
    if unknown:
        key = min(unknown, key=lambda item: str(item))
        prefix = "" if field_path == "case" else f"{field_path}."
        raise EvidenceCaseValidationError(f"{case_id}: unknown field {prefix}{key}")


def _validate_parameter_flags(intervention: dict[str, Any], *, case_id: str) -> None:
    for parameter_name, parameter in (intervention.get("parameters") or {}).items():
        if "reported" in parameter and not isinstance(parameter["reported"], bool):
            raise EvidenceCaseValidationError(
                f"{case_id}: intervention.parameters.{parameter_name}.reported must "
                "be a boolean"
            )


def _validate_case_type_contract(case: dict[str, Any]) -> None:
    case_id = str(case["case_id"])
    case_type = case["case_type"]

    if case_type != "initial":
        raise EvidenceCaseValidationError(
            f"{case_id}: only initial EvidenceCases are supported"
        )


def _validate_provenance(
    case: dict[str, Any], *, span_documents: dict[str, str]
) -> set[str]:
    case_id = str(case["case_id"])
    referenced: set[str] = set()

    for section_name in ("observed_facts", "outcomes"):
        for field_name, fact in (case.get(section_name) or {}).items():
            field_path = f"{section_name}.{field_name}"
            span_ids = list(fact.get("evidence_span_ids") or [])
            if _has_numeric_evidence(fact) and not span_ids:
                raise EvidenceCaseValidationError(
                    f"{case_id}: numeric {field_path} requires evidence_span_ids"
                )
            _require_known_spans(case_id, field_path, span_ids, span_documents)
            referenced.update(span_ids)

    for parameter_name, parameter in (
        (case.get("intervention") or {}).get("parameters", {}).items()
    ):
        field_path = f"intervention.parameters.{parameter_name}"
        for required_field in ("value", "reported"):
            if required_field not in parameter:
                raise EvidenceCaseValidationError(
                    f"{case_id}: {field_path}.{required_field} is required for "
                    "approved cases"
                )
        span_ids = list(parameter.get("evidence_span_ids") or [])
        if parameter.get("reported") is True and not span_ids:
            raise EvidenceCaseValidationError(
                f"{case_id}: reported parameter {parameter_name} requires "
                "evidence_span_ids"
            )
        _require_known_spans(case_id, field_path, span_ids, span_documents)
        referenced.update(span_ids)

    seen_claim_ids: set[str] = set()
    for index, claim in enumerate(case.get("rationale_claims") or []):
        field_path = f"rationale_claims[{index}]"
        for required_field in ("claim_id", "statement", "explicitness", "effect_area"):
            if (
                not isinstance(claim.get(required_field), str)
                or not claim[required_field].strip()
            ):
                raise EvidenceCaseValidationError(
                    f"{case_id}: {field_path}.{required_field} must be a non-empty "
                    "string "
                    "for approved cases"
                )
        claim_id = str(claim["claim_id"])
        if claim_id in seen_claim_ids:
            raise EvidenceCaseValidationError(
                f"{case_id}: duplicate rationale claim_id {claim_id!r}"
            )
        seen_claim_ids.add(claim_id)

        explicitness = claim["explicitness"]
        if explicitness not in _ALLOWED_EXPLICITNESS:
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path}.explicitness must be explicit or inferred"
            )
        span_ids = list(claim.get("evidence_span_ids") or [])
        if explicitness == "explicit" and not span_ids:
            raise EvidenceCaseValidationError(
                f"{case_id}: explicit {field_path} requires evidence_span_ids"
            )
        _require_known_spans(case_id, field_path, span_ids, span_documents)
        referenced.update(span_ids)
    return referenced


def _require_known_spans(
    case_id: str,
    field_name: str,
    span_ids: Iterable[str],
    span_documents: dict[str, str],
) -> None:
    for span_id in span_ids:
        if span_id not in span_documents:
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_name} references unknown span_id {span_id!r}"
            )


def _manifest_hifiasm_cases(pack_root: Path) -> list[dict[str, Any]] | None:
    """Load v3 case records whose manifest places them in hifiasm shards."""

    manifest_path = pack_root / "cases.yaml"
    if not manifest_path.is_file():
        return None
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(manifest, dict) or not (
        manifest.get("case_schema") == ASSEMBLY_EVIDENCE_CASE_SCHEMA
        and manifest.get("version") == _EVIDENCE_MANIFEST_VERSION
    ):
        return None

    candidates = _manifest_case_list(
        manifest, source_path=manifest_path, required=False
    )
    for rel_path in manifest.get("case_files") or []:
        if not isinstance(rel_path, str):
            continue
        relative = Path(rel_path)
        if relative.parts[:2] != ("cases", "hifiasm") or ".." in relative.parts:
            continue
        shard_path = pack_root / relative
        raw = yaml.safe_load(shard_path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, list):
            raw = {"cases": raw}
        candidates.extend(
            _manifest_case_list(raw, source_path=shard_path, required=True)
        )
    return candidates


def _manifest_case_list(
    raw: Any,
    *,
    source_path: Path,
    required: bool,
) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise EvidenceCaseValidationError(f"expected a mapping in {source_path}")
    if "cases" not in raw:
        if required:
            raise EvidenceCaseValidationError(
                f"cases list is required in {source_path}"
            )
        return []
    cases = raw["cases"]
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise EvidenceCaseValidationError(
            f"cases entries must be mappings in {source_path}"
        )
    return cases


def _is_number(value: Any) -> TypeGuard[int | float]:
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
        raise EvidenceCaseValidationError(
            f"{case_id}: observed_facts must be a mapping"
        )
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
            raise EvidenceCaseValidationError(
                f"{case_id}: outcomes.{field_name} must be a mapping"
            )
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
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path}.uncertainty must be a mapping"
            )
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
                    f"{case_id}: {field_path}.uncertainty interval requires "
                    f"{', '.join(missing)}"
                )
            lower = uncertainty.get("min")
            upper = uncertainty.get("max")
            if not _is_number(lower) or not _is_number(upper):
                raise EvidenceCaseValidationError(
                    f"{case_id}: {field_path}.uncertainty min and max must be finite "
                    "numbers"
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
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path}.value must be a finite number"
        )
    if (
        value is not None
        and interval is not None
        and not interval[0] <= float(value) <= interval[1]
    ):
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path}.value must fall within its uncertainty interval"
        )

    values = [float(value)] if value is not None else []
    if interval is not None:
        values.extend(interval)
    if field_name in {"genome_size_mb", "coverage", "read_n50_kb"} and any(
        item <= 0 for item in values
    ):
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path} values must be positive"
        )
    if field_name == "heterozygosity" and any(not 0 <= item <= 1 for item in values):
        raise EvidenceCaseValidationError(
            f"{case_id}: {field_path} values must be fractions between 0 and 1"
        )
    if field_name == "ploidy" and any(
        item <= 0 or not item.is_integer() for item in values
    ):
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
