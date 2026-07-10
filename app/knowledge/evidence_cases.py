"""Filesystem-safe validation helpers for manually curated evidence cases."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

MAX_SOURCE_EXCERPT_CHARS = 2_000


class EvidenceCaseValidationError(ValueError):
    """Raised when an evidence case or one of its source documents is invalid."""


def validate_source_bundle(
    case: dict[str, Any],
    *,
    pack_root: Path,
    require_hashes: bool,
) -> list[dict[str, str]]:
    """Validate source documents and exact-quote spans without returning full text."""

    case_id = str(case.get("case_id") or "<missing-case-id>")
    source_documents = case.get("source_documents")
    if not isinstance(source_documents, dict):
        raise EvidenceCaseValidationError(f"{case_id}: source_documents must be a mapping")

    contents: dict[str, str] = {}
    for document_name in ("body", "methods"):
        document = source_documents.get(document_name)
        if not isinstance(document, dict):
            raise EvidenceCaseValidationError(f"{case_id}: source_documents.{document_name} is required")
        path_value = document.get("path")
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

    spans = source_documents.get("spans")
    if not isinstance(spans, list):
        raise EvidenceCaseValidationError(f"{case_id}: source_documents.spans must be a list")

    seen: set[str] = set()
    validated: list[dict[str, str]] = []
    for index, span in enumerate(spans):
        if not isinstance(span, dict):
            raise EvidenceCaseValidationError(f"{case_id}: source_documents.spans[{index}] must be a mapping")
        span_id = str(span.get("span_id") or "")
        if not span_id:
            raise EvidenceCaseValidationError(f"{case_id}: source_documents.spans[{index}].span_id is required")
        if span_id in seen:
            raise EvidenceCaseValidationError(f"{case_id}: duplicate span_id {span_id!r}")
        seen.add(span_id)

        document_name = span.get("document")
        if document_name not in contents:
            raise EvidenceCaseValidationError(
                f"{case_id}: span {span_id!r} references unknown document {document_name!r}"
            )
        quote = span.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            raise EvidenceCaseValidationError(f"{case_id}: span {span_id!r} quote is required")
        document_content = contents[str(document_name)]
        if quote.strip() == document_content.strip():
            raise EvidenceCaseValidationError(
                f"{case_id}: span {span_id!r} quote must not contain the complete source document"
            )
        if len(quote) > MAX_SOURCE_EXCERPT_CHARS:
            raise EvidenceCaseValidationError(
                f"{case_id}: span {span_id!r} quote exceeds the {MAX_SOURCE_EXCERPT_CHARS}-character limit"
            )
        occurrence_count = document_content.count(quote)
        if occurrence_count == 0:
            raise EvidenceCaseValidationError(
                f"{case_id}: span {span_id!r} quote was not found in {document_name}.md"
            )
        if occurrence_count > 1:
            raise EvidenceCaseValidationError(
                f"{case_id}: span {span_id!r} quote is ambiguous ({occurrence_count} occurrences)"
            )
        validated.append({"span_id": span_id, "document": str(document_name), "quote": quote})
    return validated


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


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
