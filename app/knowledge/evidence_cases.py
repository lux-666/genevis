"""Filesystem-safe validation helpers for manually curated evidence cases."""

from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_SOURCE_EXCERPT_CHARS = 2_000
MAX_SOURCE_EXCERPTS_PER_CASE = 32
MAX_SOURCE_EXCERPT_CHARS_PER_DOCUMENT = 12_000
MAX_TOTAL_SOURCE_EXCERPT_CHARS_PER_CASE = 16_000
MAX_SOURCE_REFERENCE_CHARS = 1_000
MAX_METHODS_FILE_BYTES = 256 * 1024

_EVIDENCE_SPAN_FIELDS = {"span_id", "quote"}


class EvidenceCaseValidationError(ValueError):
    """Raised when an evidence case or its methods source is invalid."""


def validate_methods_source_shape(
    case: dict[str, Any],
    *,
    required: bool,
) -> None:
    """Validate the closed methods-only source schema without reading the file."""

    case_id = str(case.get("case_id") or "<missing-case-id>")
    methods_path = case.get("methods_path")
    if methods_path is None:
        if required:
            raise EvidenceCaseValidationError(f"{case_id}: methods_path is required")
    elif not isinstance(methods_path, str) or not methods_path.strip():
        raise EvidenceCaseValidationError(f"{case_id}: methods_path is required")
    elif len(methods_path) > MAX_SOURCE_REFERENCE_CHARS:
        raise EvidenceCaseValidationError(
            f"{case_id}: methods_path exceeds the {MAX_SOURCE_REFERENCE_CHARS}-character limit"
        )

    spans = case.get("evidence_spans")
    if spans is None:
        if required:
            raise EvidenceCaseValidationError(f"{case_id}: evidence_spans is required")
        return
    if not isinstance(spans, list):
        raise EvidenceCaseValidationError(f"{case_id}: evidence_spans must be a list")
    if len(spans) > MAX_SOURCE_EXCERPTS_PER_CASE:
        raise EvidenceCaseValidationError(
            f"{case_id}: evidence_spans may contain at most "
            f"{MAX_SOURCE_EXCERPTS_PER_CASE} spans"
        )

    seen: set[str] = set()
    total_quote_chars = 0
    for index, span in enumerate(spans):
        field_path = f"evidence_spans[{index}]"
        if not isinstance(span, dict):
            raise EvidenceCaseValidationError(f"{case_id}: {field_path} must be a mapping")
        _reject_unknown_fields(
            span,
            allowed=_EVIDENCE_SPAN_FIELDS,
            case_id=case_id,
            field_path=field_path,
        )
        span_id = span.get("span_id")
        if not isinstance(span_id, str) or not span_id.strip():
            raise EvidenceCaseValidationError(f"{case_id}: {field_path}.span_id is required")
        if len(span_id) > MAX_SOURCE_REFERENCE_CHARS:
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path}.span_id exceeds the "
                f"{MAX_SOURCE_REFERENCE_CHARS}-character limit"
            )
        if span_id in seen:
            raise EvidenceCaseValidationError(f"{case_id}: duplicate span_id {span_id!r}")
        seen.add(span_id)

        quote = span.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            raise EvidenceCaseValidationError(f"{case_id}: span {span_id!r} quote is required")
        if len(quote) > MAX_SOURCE_EXCERPT_CHARS:
            raise EvidenceCaseValidationError(
                f"{case_id}: span {span_id!r} quote exceeds the "
                f"{MAX_SOURCE_EXCERPT_CHARS}-character limit"
            )
        total_quote_chars += len(quote)

    if total_quote_chars > MAX_TOTAL_SOURCE_EXCERPT_CHARS_PER_CASE:
        raise EvidenceCaseValidationError(
            f"{case_id}: evidence_spans total quote characters exceed "
            f"{MAX_TOTAL_SOURCE_EXCERPT_CHARS_PER_CASE}"
        )


def validate_methods_source(
    case: dict[str, Any],
    *,
    pack_root: Path,
) -> list[dict[str, str]]:
    """Validate an approved methods file and return declared spans for projection."""

    case_id = str(case.get("case_id") or "<missing-case-id>")
    validate_methods_source_shape(case, required=True)
    methods_path = resolve_confined_methods_path(
        pack_root,
        case["methods_path"],
        case_id=case_id,
    )
    try:
        content_bytes = methods_path.read_bytes()
    except OSError as exc:
        raise EvidenceCaseValidationError(
            f"{case_id}: unable to read methods file: {case['methods_path']}"
        ) from exc
    if len(content_bytes) > MAX_METHODS_FILE_BYTES:
        raise EvidenceCaseValidationError(
            f"{case_id}: methods file exceeds the {MAX_METHODS_FILE_BYTES}-byte limit"
        )
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceCaseValidationError(f"{case_id}: methods file must be valid UTF-8") from exc
    if not content.strip():
        raise EvidenceCaseValidationError(f"{case_id}: methods file must be non-empty")

    return [
        {"span_id": span["span_id"], "document": "methods", "quote": span["quote"]}
        for span in case["evidence_spans"]
    ]


def validate_referenced_excerpt_budget(
    spans: list[dict[str, str]],
    *,
    referenced_span_ids: set[str],
    case_id: str,
) -> None:
    """Enforce the retained per-document prompt excerpt budget."""

    excerpt_chars = sum(
        len(span["quote"])
        for span in spans
        if span["span_id"] in referenced_span_ids
    )
    if excerpt_chars > MAX_SOURCE_EXCERPT_CHARS_PER_DOCUMENT:
        raise EvidenceCaseValidationError(
            f"{case_id}: referenced methods excerpts exceed the per-document "
            f"{MAX_SOURCE_EXCERPT_CHARS_PER_DOCUMENT}-character limit"
        )


def resolve_confined_methods_path(pack_root: Path, path_value: Any, *, case_id: str) -> Path:
    """Resolve a methods path while rejecting absolute paths and pack-root escape."""

    if not isinstance(path_value, str) or not path_value:
        raise EvidenceCaseValidationError(f"{case_id}: methods_path is required")
    relative = Path(path_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceCaseValidationError(
            f"{case_id}: methods_path escapes knowledge-pack root: {path_value}"
        )

    root = pack_root.resolve()
    try:
        resolved = (root / relative).resolve(strict=True)
    except FileNotFoundError as exc:
        raise EvidenceCaseValidationError(f"{case_id}: methods file not found: {path_value}") from exc
    if not resolved.is_relative_to(root):
        raise EvidenceCaseValidationError(
            f"{case_id}: methods_path escapes knowledge-pack root: {path_value}"
        )
    if not resolved.is_file():
        raise EvidenceCaseValidationError(f"{case_id}: methods path is not a regular file: {path_value}")
    return resolved


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
        raise EvidenceCaseValidationError(f"{case_id}: unknown field {field_path}.{key}")
