"""Natural-language request parsing for workflow entry."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from pydantic import ValidationError

from app.llm.factory import build_chat_model
from app.llm.prompts import load_prompt_bundle, render_prompt_messages
from app.schemas.entry import EntryRequest

_LIFECYCLE_PATTERNS = [
    r"\bcontinue\b",
    r"\bresume\b",
    r"\binspect\b",
    r"\bretry\b",
    r"\brerun\b",
    r"\brebuild\b",
    "继续",
    "恢复",
    "查看",
    "检查",
    "重试",
    "重新运行",
    "重建",
]


def reject_lifecycle_request(request_text: str) -> None:
    """Reject lifecycle requests that must not be treated as new workflow starts."""

    for pattern in _LIFECYCLE_PATTERNS:
        if re.search(pattern, request_text, flags=re.IGNORECASE):
            raise ValueError(
                "Lifecycle requests such as continue/resume/inspect/retry/rerun/rebuild "
                "are not supported by the start parser. Use a structured run request, "
                "or wait for the review/resume command surface."
            )


def parse_intent_response(response_text: str, *, defaults: Mapping[str, Any] | None = None) -> EntryRequest:
    """Parse one LLM response into a validated entry request."""

    payload = _parse_single_json_object(response_text)
    merged = _merge_defaults(payload, defaults)
    try:
        return EntryRequest.model_validate(merged)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in err.get('loc', ()))}: {err.get('msg', 'invalid value')}"
            for err in exc.errors()
        )
        raise ValueError(
            "Intent JSON did not match EntryRequest schema. "
            f"Details: {details}. Response snippet: {_snippet(response_text)}"
        ) from exc


def parse_request_with_llm(
    request_text: str,
    *,
    defaults: Mapping[str, Any] | None = None,
    model: Any | None = None,
) -> EntryRequest:
    """Use the intent prompt bundle to parse natural language outside the graph."""

    reject_lifecycle_request(request_text)
    bundle = load_prompt_bundle("intent.parse")
    messages = render_prompt_messages(
        bundle,
        {
            "request_text": request_text,
            "defaults": dict(defaults or {}),
        },
    )
    try:
        chat_model = model or build_chat_model()
        chat_model = _bind_intent_response_format(chat_model)
        response = chat_model.invoke(messages)
    except Exception as exc:
        fallback = _parse_request_deterministically(request_text, defaults=defaults)
        if fallback is not None:
            return fallback
        raise RuntimeError(f"Intent LLM call failed: {type(exc).__name__}: {exc}") from exc
    response_text = _response_to_text(response)
    request = parse_intent_response(response_text, defaults=defaults)
    request = _fill_target_species_from_text(request, request_text)
    request._intent_parse_source = "llm"
    return request


_SOURCE_PATH_SUFFIXES = (
    ".fastq.gz",
    ".fasta.gz",
    ".fna.gz",
    ".fa.gz",
    ".fq.gz",
    ".fastq",
    ".fasta",
    ".fna",
    ".fa",
    ".fq",
    ".bam",
    ".cram",
    ".hic",
    ".yaml",
    ".yml",
    ".json",
)


def _parse_request_deterministically(
    request_text: str,
    *,
    defaults: Mapping[str, Any] | None = None,
) -> EntryRequest | None:
    """Best-effort local parser for path-shaped start requests when LLM is unavailable."""

    source_paths = _extract_source_paths(request_text)
    if not source_paths:
        return None

    payload: dict[str, Any] = {"command": "run", "source_paths": source_paths}
    if any(path.lower().endswith((".yaml", ".yml", ".json")) for path in source_paths):
        if len(source_paths) != 1:
            return None
        payload.pop("source_paths")
        payload["manifest"] = source_paths[0]

    lower = request_text.lower()
    if any(token in lower for token in ("nanopore", "ont")):
        payload["platform"] = "ont"
        payload["asset_type_hint"] = "raw_long_reads"
    elif any(token in lower for token in ("cycloneseq", "cyclone sequencing", "cyclone文件")):
        payload["platform"] = "cycloneseq"
        payload["asset_type_hint"] = "raw_long_reads"

    target_species = _infer_target_species_from_text(request_text)
    if target_species:
        payload["target_species"] = target_species

    request = parse_intent_response(json.dumps(payload, ensure_ascii=False), defaults=defaults)
    request._intent_parse_source = "deterministic_fallback"
    return request


def _extract_source_paths(request_text: str) -> list[str]:
    suffix_re = "|".join(re.escape(suffix) for suffix in _SOURCE_PATH_SUFFIXES)
    pattern = re.compile(
        rf"(?P<path>(?:~|/|\.{{1,2}}/)[^\s，,；;：:。]+?(?:{suffix_re}))",
        flags=re.IGNORECASE,
    )
    paths: list[str] = []
    for match in pattern.finditer(request_text):
        path = match.group("path")
        if path not in paths:
            paths.append(path)
    return paths


def _infer_requested_stages_from_text(request_text: str) -> list[str] | None:
    lower = request_text.lower()
    only = any(token in lower for token in ("只做", "仅做", "只跑", "仅执行", "only", "just", "stage only"))
    if any(token in lower for token in ("t2t", "near-t2t", "telomere-to-telomere", "telomere rescue")):
        return ["t2t"] if only else ["read_qc", "genome_survey", "assembly", "assembly_dedup", "assembly_review", "hic_scaffolding", "t2t"]
    if "hic" in lower or "hi-c" in lower or "chromosome" in lower or "染色体" in request_text:
        return ["hic_scaffolding"] if only else ["read_qc", "genome_survey", "assembly", "assembly_dedup", "assembly_review", "hic_scaffolding"]
    if "assembly" in lower or "assemble" in lower or "组装" in request_text:
        return ["assembly"] if only else ["read_qc", "genome_survey", "assembly"]
    if "质控" in request_text or "read qc" in lower:
        return ["read_qc"]
    return None


_TARGET_SPECIES_PATTERNS = [
    re.compile(r"以(?P<species>[^，,。；;\s]{1,80}?)为目标物种"),
    re.compile(r"目标物种(?:是|为|[:：])(?P<species>[^，,。；;\s]{1,80})"),
]


def _fill_target_species_from_text(request: EntryRequest, request_text: str) -> EntryRequest:
    if request.target_species:
        return request
    target_species = _infer_target_species_from_text(request_text)
    if not target_species:
        return request
    return request.model_copy(update={"target_species": target_species})


def _infer_target_species_from_text(request_text: str) -> str | None:
    for pattern in _TARGET_SPECIES_PATTERNS:
        match = pattern.search(request_text)
        if not match:
            continue
        value = match.group("species").strip().strip("'\"“”‘’")
        if value:
            return value
    return None


def _parse_single_json_object(response_text: str) -> dict[str, Any]:
    extracted = _extract_json_object(response_text)
    try:
        payload = json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Intent parser expected a JSON object, but decoding failed: "
            f"{exc.msg} (line {exc.lineno}, column {exc.colno}). "
            f"Response snippet: {_snippet(response_text)}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            "Intent parser expected a JSON object at top level. "
            f"Got {type(payload).__name__}. Response snippet: {_snippet(response_text)}"
        )
    return payload


def _merge_defaults(payload: Mapping[str, Any], defaults: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(payload)
    for key in ("session_id", "data_dir"):
        if key not in merged and defaults and key in defaults and defaults[key] is not None:
            merged[key] = defaults[key]
    return merged


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Intent parser received an empty LLM response.")

    if stripped.startswith("["):
        return stripped

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        fenced_text = fenced.group(1).strip()
        if fenced_text.startswith("["):
            return fenced_text
        fenced_payloads = _extract_balanced_json_objects(fenced_text)
        if len(fenced_payloads) == 1:
            return fenced_payloads[0]
        if len(fenced_payloads) > 1:
            raise ValueError("Intent parser found multiple JSON objects inside one fenced block.")
        if fenced_text.startswith("{"):
            return fenced_text

    payloads = _extract_balanced_json_objects(stripped)
    if len(payloads) == 1:
        return payloads[0]
    if len(payloads) > 1:
        raise ValueError("Intent parser found multiple JSON objects in the LLM response.")
    if stripped.startswith("{"):
        return stripped

    raise ValueError("Could not find a JSON object in the LLM response.")


def _extract_balanced_json_objects(text: str) -> list[str]:
    payloads: list[str] = []
    start_index: int | None = None
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if start_index is None:
            if char == "{":
                start_index = index
                depth = 1
            continue

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                payloads.append(text[start_index : index + 1])
                start_index = None

    return payloads


def _make_schema_strict(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively enforce OpenAI strict structured output requirements."""
    if "properties" in schema:
        schema["additionalProperties"] = False
        schema["required"] = list(schema["properties"].keys())
        for prop in schema["properties"].values():
            _make_schema_strict(prop)
    if "$defs" in schema:
        for defn in schema["$defs"].values():
            _make_schema_strict(defn)
    if "items" in schema and isinstance(schema["items"], dict):
        _make_schema_strict(schema["items"])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            for variant in schema[key]:
                if isinstance(variant, dict):
                    _make_schema_strict(variant)
    return schema


def _bind_intent_response_format(chat_model: Any) -> Any:
    bind = getattr(chat_model, "bind", None)
    if not callable(bind):
        return chat_model

    schema = _make_schema_strict(EntryRequest.model_json_schema())
    try:
        return bind(
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "entry_request",
                    "schema": schema,
                    "strict": True,
                },
            }
        )
    except Exception:
        return bind(response_format={"type": "json_object"})


def _response_to_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    return str(content)


def _snippet(text: str, limit: int = 220) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return f"{one_line[:limit]}..."
