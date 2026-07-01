"""Shared freeze/run/collect lifecycle for executable Hi-C methods."""

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
    HIC_QC_REPORT,
    HIC_REVIEW_BUNDLE,
    HIC_SCAFFOLDED_ASSEMBLY,
    ArtifactMeta,
)
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir, stage_output, update_stage_output


@dataclass(frozen=True)
class HiCMethodLifecycle:
    """State-key convention for one executable Hi-C scaffolding method."""

    name: str
    method_name: str
    frozen_status: str
    ran_status: str
    frozen_attempt_key: str
    execution_result_key: str


def build_method_graph(
    lifecycle: HiCMethodLifecycle,
    freeze: Callable[[TopLevelState], dict],
    collect: Callable[[TopLevelState], dict],
):
    """Build the reusable freeze -> run -> collect method subgraph."""
    graph = StateGraph(TopLevelState)
    graph.add_node("freeze", freeze)
    graph.add_node("run", lambda state: run_method_attempt(state, lifecycle))
    graph.add_node("collect", collect)
    graph.add_node("done", lambda state: finish_method_lifecycle(state, lifecycle))
    graph.add_edge(START, "freeze")
    graph.add_conditional_edges(
        "freeze",
        lambda state: "run" if hic_output(state).get("status") == lifecycle.frozen_status else "done",
        {"run": "run", "done": "done"},
    )
    graph.add_conditional_edges(
        "run",
        lambda state: "collect" if hic_output(state).get("status") == lifecycle.ran_status else "done",
        {"collect": "collect", "done": "done"},
    )
    graph.add_edge("collect", "done")
    graph.add_edge("done", END)
    return graph.compile()


def hic_output(state: TopLevelState) -> dict[str, Any]:
    return stage_output(state, "hic_scaffolding")


def update_hic_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    return update_stage_output(state, "hic_scaffolding", payload)


def run_method_attempt(state: TopLevelState, lifecycle: HiCMethodLifecycle) -> dict:
    """Run one frozen method attempt, preserving fallback review semantics."""
    output = hic_output(state)
    if output.get("status") != lifecycle.frozen_status:
        return {}
    attempt = FrozenAttemptSpec.model_validate(output[lifecycle.frozen_attempt_key])
    outcome = run_frozen_attempt(
        attempt,
        data_dir=absolute_data_dir(state),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label=lifecycle.name,
        cleanup_outputs=False,
    )
    result = outcome.result.model_dump(mode="json")
    if not outcome.success:
        result["error"] = outcome.error
    return update_hic_output(
        state,
        {"status": lifecycle.ran_status, lifecycle.execution_result_key: result},
    )


def finish_method_lifecycle(state: TopLevelState, lifecycle: HiCMethodLifecycle) -> dict:
    """Mark a method adapter complete so the parent can schedule the next one."""
    output = hic_output(state)
    completed = list(output.get("completed_method_names") or [])
    if lifecycle.method_name not in completed:
        completed.append(lifecycle.method_name)
    return update_hic_output(state, {"completed_method_names": completed})


def collect_method_artifacts(
    state: TopLevelState,
    lifecycle: HiCMethodLifecycle,
    *,
    extra_artifacts: list[tuple[str, str]] = (),
    execution_metadata: dict[str, Any] | None = None,
) -> dict:
    """Register a Hi-C candidate, logs, review bundle, and fallback state."""
    output = hic_output(state)
    if output.get("status") != lifecycle.ran_status:
        return {}

    attempt = FrozenAttemptSpec.model_validate(output[lifecycle.frozen_attempt_key])
    result = dict(output.get(lifecycle.execution_result_key) or {})
    data_dir = absolute_data_dir(state)
    artifact_dir = Path(str(attempt.metadata["artifact_dir"]))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[ArtifactMeta] = []
    notes = list(output.get("notes") or [])

    if result.get("success"):
        scaffold_fasta = Path(str(attempt.metadata["scaffold_fasta"]))
        if not scaffold_fasta.exists():
            reason = f"expected {lifecycle.name} scaffold assembly missing: {scaffold_fasta}"
            Ledger(data_dir).record_attempt_failed(attempt.session_id, attempt, return_code=0, error=reason)
            result = {**result, "success": False, "error": reason}
        else:
            artifacts.append(
                ArtifactMeta.from_file(
                    scaffold_fasta,
                    stage="hic_scaffolding",
                    method_name=lifecycle.method_name,
                    artifact_type=HIC_SCAFFOLDED_ASSEMBLY,
                    metadata={"attempt_id": attempt.attempt_id, "review_required": True},
                )
            )

    if result.get("success"):
        for key, artifact_type in extra_artifacts:
            path_value = attempt.metadata.get(key)
            if path_value and Path(str(path_value)).exists():
                artifacts.append(
                    ArtifactMeta.from_file(
                        Path(str(path_value)),
                        stage="hic_scaffolding",
                        method_name=lifecycle.method_name,
                        artifact_type=artifact_type,
                        metadata={"attempt_id": attempt.attempt_id, "review_required": True},
                    )
                )

        review_bundle_ref = artifact_dir / "hic_review_bundle.json"
        review_bundle_ref.write_text(
            json.dumps(
                {
                    "session_id": attempt.session_id,
                    "method_name": lifecycle.method_name,
                    "status": f"{lifecycle.name}_executed_review_required",
                    "pre_hic_assembly_ref": output.get("pre_hic_assembly_ref"),
                    "final_assembly_for_annotation": output.get("final_assembly_for_annotation"),
                    "chromosome_scaffolded_assembly_candidate_ref": attempt.metadata.get("scaffold_fasta"),
                    "stdout_path": result.get("stdout_path"),
                    "stderr_path": result.get("stderr_path"),
                    "review_required": True,
                    "execution_metadata": execution_metadata or {},
                    "attempt_metadata": attempt.metadata,
                    "notes": [
                        "contact_map_policy_required_before_promotion",
                        "pre_hic_assembly_preserved_as_annotation_fallback",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        qc_report_ref = artifact_dir / "hic_qc_report.json"
        qc_report_ref.write_text(
            json.dumps(
                {
                    "session_id": attempt.session_id,
                    "method_name": lifecycle.method_name,
                    "status": "available",
                    "pre_hic_assembly_ref": output.get("pre_hic_assembly_ref"),
                    "chromosome_scaffolded_assembly_candidate_ref": attempt.metadata.get("scaffold_fasta"),
                    "return_code": result.get("return_code"),
                    "execution_metadata": execution_metadata or {},
                    "notes": ["qc_thresholds_not_applied", "contact_map_policy_required_before_promotion"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        artifacts.extend(
            [
                ArtifactMeta.from_file(
                    review_bundle_ref,
                    stage="hic_scaffolding",
                    method_name=lifecycle.method_name,
                    artifact_type=HIC_REVIEW_BUNDLE,
                    metadata={"attempt_id": attempt.attempt_id, "review_required": True},
                ),
                ArtifactMeta.from_file(
                    qc_report_ref,
                    stage="hic_scaffolding",
                    method_name=lifecycle.method_name,
                    artifact_type=HIC_QC_REPORT,
                    metadata={"attempt_id": attempt.attempt_id, "review_required": True},
                ),
            ]
        )
        notes = unique_refs([*notes, f"{lifecycle.name}_executed", "contact_map_policy_required_before_promotion"])
        candidates = dict(output.get("method_candidates") or {})
        candidates[lifecycle.method_name] = {
            "status": "executed",
            "scaffold_fasta": str(attempt.metadata["scaffold_fasta"]),
            "review_bundle_ref": str(review_bundle_ref),
            "qc_report_ref": str(qc_report_ref),
        }
        update = {
            "status": "blocked_human",
            "chromosome_scaffolded_assembly_candidate_ref": str(attempt.metadata["scaffold_fasta"]),
            "hic_review_bundle_ref": str(review_bundle_ref),
            "hic_qc_report_ref": str(qc_report_ref),
            "selected_method_name": lifecycle.method_name,
            "notes": notes,
            "method_candidates": candidates,
        }
    else:
        notes = unique_refs([*notes, f"{lifecycle.name}_execution_failed", "annotation_continues_on_pre_hic_assembly_fallback"])
        candidates = dict(output.get("method_candidates") or {})
        candidates[lifecycle.method_name] = {"status": "failed", "error": result.get("error")}
        update = {
            "status": "blocked_human",
            "selected_method_name": lifecycle.method_name,
            "notes": notes,
            f"{lifecycle.name}_error": result.get("error"),
            "method_candidates": candidates,
        }

    for path_key in ("stdout_path", "stderr_path"):
        path = Path(str(result.get(path_key, "")))
        if path.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage="hic_scaffolding",
                    method_name=lifecycle.method_name,
                    artifact_type="command.stdout" if path_key == "stdout_path" else "command.stderr",
                    metadata={"attempt_id": attempt.attempt_id},
                )
            )

    if artifacts:
        append_artifact_records(data_dir, attempt.session_id, artifacts)
        payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
        ledger = Ledger(data_dir)
        if result.get("success"):
            ledger.record_attempt_succeeded(attempt.session_id, attempt, artifacts=payloads)
        for artifact in payloads:
            ledger.record_artifact_registered(attempt.session_id, artifact, attempt_id=attempt.attempt_id)

    return update_hic_output(
        state,
        {
            **update,
            "artifact_refs": unique_refs([*list(output.get("artifact_refs") or []), *[str(a.file_path) for a in artifacts]]),
            lifecycle.frozen_attempt_key: None,
            lifecycle.execution_result_key: None,
        },
    )


def unique_refs(refs: list[str]) -> list[str]:
    return list(dict.fromkeys(refs))
