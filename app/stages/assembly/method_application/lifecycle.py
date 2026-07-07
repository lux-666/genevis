"""Shared lifecycle primitives for executable assembly-method subgraphs.

Each method module owns how an attempt is frozen.  This module owns the
method-independent execution, artifact registration, and state transition
protocol so another method (for example hifiasm) does not need to copy it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.execution.method_runner import run_frozen_attempt
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ArtifactMeta,
    AssemblyCandidateBundle,
)
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState


@dataclass(frozen=True)
class MethodLifecycle:
    """State-key convention for one executable assembly method."""

    name: str
    method_name: str
    planned_status: str
    frozen_status: str
    ran_status: str
    frozen_attempt_key: str
    execution_result_key: str
    draft_assembly_key: str
    substep_key: str


def build_method_graph(
    lifecycle: MethodLifecycle,
    freeze: Callable[[TopLevelState], dict],
    collect: Callable[[TopLevelState], dict],
):
    """Build the common freeze → run → collect method-application graph."""
    graph = StateGraph(TopLevelState)
    graph.add_node("freeze", freeze)
    graph.add_node("run", lambda state: run_method_attempt(state, lifecycle))
    graph.add_node("collect", collect)
    graph.add_node("done", lambda _state: {})
    graph.add_edge(START, "freeze")
    graph.add_conditional_edges(
        "freeze",
        lambda state: "run" if assembly_output(state).get("status") == lifecycle.frozen_status else "done",
        {"run": "run", "done": "done"},
    )
    graph.add_conditional_edges(
        "run",
        lambda state: "collect" if assembly_output(state).get("status") == lifecycle.ran_status else "done",
        {"collect": "collect", "done": "done"},
    )
    graph.add_edge("collect", "done")
    graph.add_edge("done", END)
    return graph.compile()


def assembly_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("assembly", {}))


def update_assembly_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["assembly"] = {**assembly_output(state), **payload}
    return {"stage_outputs": outputs}


def fail_update(state: TopLevelState, reason: str) -> dict:
    return update_assembly_output(state, {"status": "failed", "reason": reason})


def run_method_attempt(state: TopLevelState, lifecycle: MethodLifecycle) -> dict:
    """Execute the frozen attempt and persist a method-namespaced result."""
    output = assembly_output(state)
    if output.get("status") != lifecycle.frozen_status:
        return {}

    attempt = FrozenAttemptSpec.model_validate(output[lifecycle.frozen_attempt_key])
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label=lifecycle.name,
    )
    result = outcome.result.model_dump(mode="json")
    parameter_attempts = _update_current_parameter_attempt(
        output,
        status="execution_failed" if not outcome.success else "executed",
        execution_result=result,
    )
    if not outcome.success:
        return update_assembly_output(
            state,
            {
                "status": "failed",
                "reason": outcome.error,
                lifecycle.execution_result_key: result,
                "parameter_attempts": parameter_attempts,
            },
        )
    return update_assembly_output(
        state,
        {"status": lifecycle.ran_status, lifecycle.execution_result_key: result, "parameter_attempts": parameter_attempts},
    )


def collect_method_artifacts(
    state: TopLevelState,
    lifecycle: MethodLifecycle,
    *,
    extra_artifacts: list[tuple[Path, str]],
    bundle_logs: dict[str, str],
    bundle_metadata: dict[str, Any],
) -> dict:
    """Register standard artifacts and materialize the selected candidate bundle."""
    output = assembly_output(state)
    if output.get("status") != lifecycle.ran_status:
        return {}

    attempt = FrozenAttemptSpec.model_validate(output[lifecycle.frozen_attempt_key])
    result = output[lifecycle.execution_result_key]
    draft_assembly = Path(output[lifecycle.draft_assembly_key])
    if not draft_assembly.exists():
        reason = f"expected {lifecycle.name} draft assembly missing: {draft_assembly}"
        Ledger(state.get("data_dir", "data")).record_attempt_failed(
            attempt.session_id, attempt, return_code=0, error=reason
        )
        return update_assembly_output(state, {"status": "failed", "reason": reason})

    artifacts = [
        ArtifactMeta.from_file(
            draft_assembly,
            stage="assembly",
            method_name=lifecycle.method_name,
            artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
            metadata={"attempt_id": attempt.attempt_id, "candidate_id": f"{attempt.session_id}:{lifecycle.method_name}"},
        )
    ]
    for path, artifact_type in [
        (Path(result["stdout_path"]), "command.stdout"),
        (Path(result["stderr_path"]), "command.stderr"),
        *extra_artifacts,
    ]:
        if path.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage="assembly",
                    method_name=lifecycle.method_name,
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt.attempt_id},
                )
            )

    bundle_path = _candidate_bundle_path(output, lifecycle.method_name)
    if bundle_path is None:
        return update_assembly_output(
            state, {"status": "failed", "reason": f"missing {lifecycle.name} candidate bundle ref"}
        )
    bundle_data = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle_data["status"] = "executed"
    bundle_data["expected_outputs"] = {
        **dict(bundle_data.get("expected_outputs") or {}),
        "draft_assembly_fasta": str(draft_assembly),
    }
    bundle_data["logs"] = {
        **dict(bundle_data.get("logs") or {}),
        "stdout": str(result["stdout_path"]),
        "stderr": str(result["stderr_path"]),
        **bundle_logs,
    }
    bundle_data["metadata"] = {
        **dict(bundle_data.get("metadata") or {}),
        "execution_enabled": True,
        "attempt_id": attempt.attempt_id,
        "draft_assembly_artifact_ref": str(draft_assembly),
        "draft_assembly_ref_status": "materialized",
        **bundle_metadata,
    }
    bundle = AssemblyCandidateBundle.model_validate(bundle_data)
    bundle_path.write_text(json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append(
        ArtifactMeta.from_file(
            bundle_path,
            stage="assembly",
            method_name=lifecycle.method_name,
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
            metadata={"candidate_id": bundle.candidate_id, "execution_enabled": True, "attempt_id": attempt.attempt_id},
        )
    )

    append_artifact_records(state.get("data_dir", "data"), attempt.session_id, artifacts)
    ledger = Ledger(state.get("data_dir", "data"))
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(attempt.session_id, attempt, artifacts=artifact_payloads)
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(attempt.session_id, artifact, attempt_id=attempt.attempt_id)

    substeps = dict(output.get("substeps") or {})
    substeps[lifecycle.substep_key] = {
        "name": lifecycle.substep_key,
        "status": "executed",
        "method_name": lifecycle.method_name,
        "draft_assembly_ref": str(draft_assembly),
        "candidate_ref": str(bundle_path),
    }
    return update_assembly_output(
        state,
        {
            "status": "draft_ready",
            "draft_assembly_ref": str(draft_assembly),
            "candidate_assembly_refs": [str(draft_assembly)],
            "executed_candidate_refs": [str(bundle_path)],
            "artifact_refs": unique_refs([*list(output.get("artifact_refs") or []), *[str(item.file_path) for item in artifacts]]),
            "candidate_artifact_refs": unique_refs([*list(output.get("candidate_artifact_refs") or []), str(bundle_path)]),
            "substeps": substeps,
        },
    )


def unique_refs(refs: list[str]) -> list[str]:
    return list(dict.fromkeys(refs))


def _update_current_parameter_attempt(output: dict[str, Any], **updates: Any) -> list[dict[str, Any]]:
    if not output.get("parameter_attempts"):
        return []
    attempt_id = int(output.get("current_parameter_attempt_id") or 0)
    attempts: list[dict[str, Any]] = []
    found = False
    for item in output.get("parameter_attempts") or []:
        if not isinstance(item, dict):
            continue
        current = dict(item)
        if current.get("attempt_id") == attempt_id:
            current.update(updates)
            found = True
        attempts.append(current)
    if not found:
        attempts.append({"attempt_id": attempt_id, **updates})
    return attempts


def positive_int(
    value: Any,
    name: str,
    missing: list[str],
    invalid: list[dict[str, Any]],
) -> int | None:
    """Parse a positive integer while preserving structured validation errors."""
    if value is None or value == "":
        missing.append(name)
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        invalid.append({"param": name, "value": value, "reason": "must be a positive integer"})
        return None
    if converted <= 0:
        invalid.append({"param": name, "value": value, "reason": "must be a positive integer"})
    return converted


def blocked_parameters(
    prefix: str,
    method_name: str,
    missing: list[str],
    invalid: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create the shared blocked-param state payload."""
    details = []
    if missing:
        details.append("missing: " + ", ".join(missing))
    if invalid:
        details.append("invalid: " + ", ".join(item["param"] for item in invalid))
    return {
        "status": "blocked_param",
        "reason": f"{prefix} incomplete ({'; '.join(details)})",
        "missing_params": missing,
        "invalid_params": invalid,
        "method_name": method_name,
    }


def _candidate_bundle_path(output: dict[str, Any], method_name: str) -> Path | None:
    for ref in output.get("selected_candidate_refs") or []:
        path = Path(ref)
        if path.exists() and json.loads(path.read_text(encoding="utf-8")).get("method_name") == method_name:
            return path
    return None
