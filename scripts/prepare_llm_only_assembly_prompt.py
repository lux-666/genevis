#!/usr/bin/env python3
"""Prepare an Assembly LLM-only request from a frozen Gene Agent run.

The source run supplies the exact persisted Assembly model request.  This
command removes curated literature and mechanism guidance, validates the
remaining DTO, and writes the provider-neutral system/user messages that can
be pasted into an external LLM.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.llm.parameters.assembly.prompt import render_assembly_parameter_messages
from app.llm.parameters.assembly.request import AssemblyParameterRequestDTO
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_COMMAND,
    ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
    ASSEMBLY_PARAMETER_MODEL_REQUEST,
)
from app.storage.artifacts import read_artifact_records, resolve_artifact_ref
from app.storage.paths import SessionPaths


def _records(run_dir: Path) -> list[dict[str, Any]]:
    paths = SessionPaths.from_run_dir(run_dir)
    return read_artifact_records(paths.run_dir, paths.session_id)


def _latest_record(run_dir: Path, artifact_type: str) -> dict[str, Any] | None:
    records = [
        record
        for record in _records(run_dir)
        if record.get("artifact_type") == artifact_type
    ]
    return records[-1] if records else None


def _latest_request_record(run_dir: Path) -> dict[str, Any]:
    records = _records(run_dir)
    matches = [
        record
        for record in records
        if record.get("artifact_type") == ASSEMBLY_PARAMETER_MODEL_REQUEST
    ]
    if not matches:
        paths = SessionPaths.from_run_dir(run_dir)
        raise SystemExit(
            "No Assembly parameter model_request artifact found in "
            f"{paths.run_dir}. The run must have reached Assembly parameter planning."
        )
    # The artifact index is append-only; the final matching record is the
    # newest occurrence/attempt.
    return matches[-1]


def _load_request(run_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    paths = SessionPaths.from_run_dir(run_dir)
    ref = {
        "schema_version": 2,
        "artifact_id": record["artifact_id"],
        "path_ref": record["path_ref"],
        "sha256": record["sha256"],
        "size_bytes": record["size_bytes"],
        "artifact_type": record["artifact_type"],
    }
    request_path = resolve_artifact_ref(paths.run_dir, paths.session_id, ref)
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"model request is not an object: {request_path}")
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise ValueError(f"model request does not contain two messages: {request_path}")
    user_content = messages[1].get("content") if isinstance(messages[1], dict) else None
    if not isinstance(user_content, str):
        raise ValueError(f"model request user message has no text: {request_path}")
    request_dto = json.loads(user_content)
    if not isinstance(request_dto, dict):
        raise ValueError("model request user content is not a JSON object")
    return request_dto


def _load_indexed_json(run_dir: Path, record: dict[str, Any] | None) -> Any:
    if record is None:
        return None
    paths = SessionPaths.from_run_dir(run_dir)
    ref = {
        field: record[field]
        for field in (
            "schema_version",
            "artifact_id",
            "path_ref",
            "sha256",
            "size_bytes",
            "artifact_type",
        )
    }
    path = resolve_artifact_ref(paths.run_dir, paths.session_id, ref)
    return json.loads(path.read_text(encoding="utf-8"))


def prepare_request(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    record = _latest_request_record(run_dir)
    request = _load_request(run_dir, record)
    removed = {
        field: request.pop(field, None)
        for field in ("knowledge_evidence", "method_guidance", "method_decision")
        if field in request
    }
    request.update(knowledge_evidence={}, method_guidance=[], method_decision={})
    validated = AssemblyParameterRequestDTO.model_validate(request).model_dump(
        mode="json", exclude_none=True
    )
    messages = render_assembly_parameter_messages(validated)
    metadata = {
        "source_run": str(SessionPaths.from_run_dir(run_dir).run_dir),
        "source_artifact_type": ASSEMBLY_PARAMETER_MODEL_REQUEST,
        "removed_knowledge_fields": {
            key: bool(value) for key, value in removed.items()
        },
        "method_id": validated["method_id"],
        "task": validated["task"],
    }
    paths = SessionPaths.from_run_dir(run_dir)
    operator_context = {
        "source_run": str(paths.run_dir),
        "input_manifest": (
            paths.input_manifest_path.read_text(encoding="utf-8")
            if paths.input_manifest_path.exists()
            else None
        ),
        "run_config": (
            json.loads(paths.run_config_path.read_text(encoding="utf-8"))
            if paths.run_config_path.exists()
            else None
        ),
        "frozen_parameters": _load_indexed_json(
            run_dir, _latest_record(run_dir, ASSEMBLY_PARAMETER_FROZEN_PARAMETERS)
        ),
        "command": _load_indexed_json(
            run_dir, _latest_record(run_dir, ASSEMBLY_PARAMETER_COMMAND)
        ),
    }
    return (
        {"metadata": metadata, "messages": messages, "request": validated},
        operator_context,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path, help="<data-root>/runs/<session-id>")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("assembly_llm_only_request.json"),
    )
    args = parser.parse_args()
    prepared, operator_context = prepare_request(args.run_dir.expanduser().resolve())
    output_path = args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(prepared, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    context_path = output_path.with_name(output_path.stem + ".context.json")
    context_path.write_text(
        json.dumps(operator_context, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output_path}")
    print(f"wrote operator context {context_path}")
    print(f"method: {prepared['metadata']['method_id']}")
    print(
        "knowledge fields cleared: knowledge_evidence, method_decision, method_guidance"
    )
    print(
        "send only the messages array to the LLM; keep the context file for "
        "command construction"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
