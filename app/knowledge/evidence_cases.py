"""Filesystem-safe validation helpers for manually curated evidence cases."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

MAX_SOURCE_EXCERPT_CHARS = 2_000
MAX_SOURCE_EXCERPTS_PER_CASE = 32
MAX_SOURCE_EXCERPT_CHARS_PER_DOCUMENT = 12_000
MAX_TOTAL_SOURCE_EXCERPT_CHARS_PER_CASE = 16_000
MAX_SOURCE_EXCERPT_COVERAGE_RATIO = 0.90
MAX_SOURCE_REFERENCE_CHARS = 1_000

_SOURCE_DOCUMENT_NAMES = ("body", "methods")
_SOURCE_BUNDLE_FIELDS = {"body", "methods", "spans"}
_SOURCE_DOCUMENT_FIELDS = {"path", "sha256"}
_SOURCE_SPAN_FIELDS = {"span_id", "document", "quote"}


class EvidenceCaseValidationError(ValueError):
    """Raised when an evidence case or one of its source documents is invalid."""


def validate_source_bundle_shape(
    case: dict[str, Any],
    *,
    required: bool,
) -> None:
    """Validate the closed source-document schema without reading document bodies."""

    case_id = str(case.get("case_id") or "<missing-case-id>")
    source_documents = case.get("source_documents")
    if source_documents is None and not required:
        return
    if not isinstance(source_documents, dict):
        raise EvidenceCaseValidationError(f"{case_id}: source_documents must be a mapping")
    _reject_unknown_fields(
        source_documents,
        allowed=_SOURCE_BUNDLE_FIELDS,
        case_id=case_id,
        field_path="source_documents",
    )

    for document_name in _SOURCE_DOCUMENT_NAMES:
        document = source_documents.get(document_name)
        if document is None and not required:
            continue
        if not isinstance(document, dict):
            raise EvidenceCaseValidationError(f"{case_id}: source_documents.{document_name} is required")
        field_path = f"source_documents.{document_name}"
        _reject_unknown_fields(
            document,
            allowed=_SOURCE_DOCUMENT_FIELDS,
            case_id=case_id,
            field_path=field_path,
        )
        path_value = document.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise EvidenceCaseValidationError(f"{case_id}: {field_path}.path is required")
        if len(path_value) > MAX_SOURCE_REFERENCE_CHARS:
            raise EvidenceCaseValidationError(
                f"{case_id}: {field_path}.path exceeds the "
                f"{MAX_SOURCE_REFERENCE_CHARS}-character limit"
            )
        expected_hash = document.get("sha256")
        if expected_hash is not None and not _is_sha256(expected_hash):
            raise EvidenceCaseValidationError(f"{case_id}: {field_path}.sha256 must be a SHA256 hex digest")

    spans = source_documents.get("spans")
    if spans is None and not required:
        return
    if not isinstance(spans, list):
        raise EvidenceCaseValidationError(f"{case_id}: source_documents.spans must be a list")
    if len(spans) > MAX_SOURCE_EXCERPTS_PER_CASE:
        raise EvidenceCaseValidationError(
            f"{case_id}: source_documents.spans may contain at most "
            f"{MAX_SOURCE_EXCERPTS_PER_CASE} spans"
        )

    seen: set[str] = set()
    total_quote_chars = 0
    for index, span in enumerate(spans):
        field_path = f"source_documents.spans[{index}]"
        if not isinstance(span, dict):
            raise EvidenceCaseValidationError(f"{case_id}: {field_path} must be a mapping")
        _reject_unknown_fields(
            span,
            allowed=_SOURCE_SPAN_FIELDS,
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
        if span.get("document") not in _SOURCE_DOCUMENT_NAMES:
            raise EvidenceCaseValidationError(
                f"{case_id}: span {span_id!r} references unknown document {span.get('document')!r}"
            )
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
            f"{case_id}: source_documents.spans total quote characters exceed "
            f"{MAX_TOTAL_SOURCE_EXCERPT_CHARS_PER_CASE}"
        )


def validate_source_bundle(
    case: dict[str, Any],
    *,
    pack_root: Path,
    require_hashes: bool,
) -> list[dict[str, Any]]:
    """Validate source documents and exact-quote spans without returning full text."""

    case_id = str(case.get("case_id") or "<missing-case-id>")
    validate_source_bundle_shape(case, required=True)
    source_documents = case["source_documents"]

    contents: dict[str, str] = {}
    for document_name in _SOURCE_DOCUMENT_NAMES:
        document = source_documents[document_name]
        path_value = document["path"]
        source_path = resolve_confined_source_path(pack_root, path_value, case_id=case_id)
        content = source_path.read_text(encoding="utf-8")
        expected_hash = document.get("sha256")
        if require_hashes and not _is_sha256(expected_hash):
            raise EvidenceCaseValidationError(
                f"{case_id}: source_documents.{document_name}.sha256 is required for approved cases"
            )
        if expected_hash:
            actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            if actual_hash != str(expected_hash).lower():
                raise EvidenceCaseValidationError(
                    f"{case_id}: source_documents.{document_name}.sha256 checksum mismatch"
                )
        contents[document_name] = content

    validated: list[dict[str, Any]] = []
    for span in source_documents["spans"]:
        span_id = span["span_id"]
        document_name = span["document"]
        quote = span["quote"]
        document_content = contents[document_name]
        if quote.strip() == document_content.strip():
            raise EvidenceCaseValidationError(
                f"{case_id}: span {span_id!r} quote must not contain the complete source document"
            )
        offset = _unique_quote_offset(document_content, quote)
        if offset is None:
            raise EvidenceCaseValidationError(
                f"{case_id}: span {span_id!r} quote was not found in {document_name}.md"
            )
        if offset < 0:
            raise EvidenceCaseValidationError(
                f"{case_id}: span {span_id!r} quote is ambiguous (multiple occurrences)"
            )
        validated.append(
            {
                "span_id": span_id,
                "document": document_name,
                "quote": quote,
                "_start": offset,
                "_end": offset + len(quote),
                "_document_chars": len(document_content),
            }
        )
    return validated


def validate_source_excerpt_coverage(
    spans: list[dict[str, Any]],
    *,
    referenced_span_ids: set[str],
    case_id: str,
) -> None:
    """Reject excessive cumulative or merged excerpt coverage per source document."""

    by_document: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        if span["span_id"] in referenced_span_ids:
            by_document.setdefault(span["document"], []).append(span)

    for document_name, document_spans in by_document.items():
        excerpt_chars = sum(len(span["quote"]) for span in document_spans)
        if excerpt_chars > MAX_SOURCE_EXCERPT_CHARS_PER_DOCUMENT:
            raise EvidenceCaseValidationError(
                f"{case_id}: referenced {document_name} excerpts exceed the per-document "
                f"{MAX_SOURCE_EXCERPT_CHARS_PER_DOCUMENT}-character limit"
            )

        intervals = sorted((span["_start"], span["_end"]) for span in document_spans)
        covered_chars = _merged_interval_chars(intervals)
        document_chars = document_spans[0]["_document_chars"]
        if covered_chars == document_chars:
            raise EvidenceCaseValidationError(
                f"{case_id}: referenced {document_name} excerpts cover the complete source document"
            )
        coverage_ratio = covered_chars / document_chars if document_chars else 1.0
        if coverage_ratio >= MAX_SOURCE_EXCERPT_COVERAGE_RATIO:
            raise EvidenceCaseValidationError(
                f"{case_id}: referenced {document_name} excerpt coverage "
                f"{coverage_ratio:.1%} exceeds the per-document limit of "
                f"{MAX_SOURCE_EXCERPT_COVERAGE_RATIO:.0%}"
            )


def _unique_quote_offset(document_content: str, quote: str) -> int | None:
    """Return a unique offset, None for absent, or -1 for overlapping ambiguity."""

    first = document_content.find(quote)
    if first < 0:
        return None
    if document_content.find(quote, first + 1) >= 0:
        return -1
    return first


def _merged_interval_chars(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    covered = 0
    start, end = intervals[0]
    for next_start, next_end in intervals[1:]:
        if next_start <= end:
            end = max(end, next_end)
            continue
        covered += end - start
        start, end = next_start, next_end
    return covered + end - start


def resolve_confined_source_path(pack_root: Path, path_value: Any, *, case_id: str) -> Path:
    """Resolve a source path while rejecting absolute paths and pack-root escape."""

    if not isinstance(path_value, str) or not path_value:
        raise EvidenceCaseValidationError(f"{case_id}: source document path is required")
    relative = Path(path_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceCaseValidationError(f"{case_id}: source path escapes knowledge-pack root: {path_value}")

    root = pack_root.resolve()
    try:
        resolved = (root / relative).resolve(strict=True)
    except FileNotFoundError as exc:
        raise EvidenceCaseValidationError(f"{case_id}: source document not found: {path_value}") from exc
    if not resolved.is_relative_to(root):
        raise EvidenceCaseValidationError(f"{case_id}: source path escapes knowledge-pack root: {path_value}")
    if not resolved.is_file():
        raise EvidenceCaseValidationError(f"{case_id}: source document is not a file: {path_value}")
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


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
