"""Shared state and execution helpers for assembly-review metric methods.

This module is deliberately independent of the parent stage nodes.  Metric
subgraphs own their command lifecycle; the parent graph only plans and routes
the review workflow.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.execution.artifacts import append_artifact_records
from app.execution.env_dispatch import resolve_method_env
from app.execution.evidence import load_frozen_attempt, persist_execution_result
from app.execution.method_runner import run_frozen_attempt
from app.schemas.artifacts import (
    ASSEMBLY_REVIEW_EVAL_BUNDLE,
    ArtifactMeta,
    AssemblyEvalBundle,
    AssemblyEvalSignal,
)
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir, stage_artifacts_dir, stage_work_dir
from app.storage.publish import publish_file

ASSEMBLY_REVIEW_CONTRACT_METHOD = "assembly_review.contract"


def build_method_graph(
    freeze: Callable[[TopLevelState], dict],
    run: Callable[[TopLevelState], dict],
    collect: Callable[[TopLevelState], dict],
    *,
    frozen_status: str,
    ran_status: str,
):
    """Build the common freeze -> run -> collect metric graph."""
    graph = StateGraph(TopLevelState)
    graph.add_node("freeze", freeze)
    graph.add_node("run", run)
    graph.add_node("collect", collect)
    graph.add_node("done", lambda _state: {})
    graph.add_edge(START, "freeze")
    graph.add_conditional_edges("freeze", lambda state: "run" if status(state) == frozen_status else "done", {"run": "run", "done": "done"})
    graph.add_conditional_edges("run", lambda state: "collect" if status(state) == ran_status else "done", {"collect": "collect", "done": "done"})
    graph.add_edge("collect", "done")
    graph.add_edge("done", END)
    return graph.compile()


def review_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("assembly_review") or {})


def status(state: TopLevelState) -> str | None:
    return review_output(state).get("status")


def update_review_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["assembly_review"] = {**review_output(state), **payload}
    return {"stage_outputs": outputs}


def absolute_review_data_dir(state: TopLevelState) -> Path:
    return absolute_data_dir(state)


def resolve_review_method_env(output: dict[str, Any], method_name: str, *, session_id: str, data_dir: Path, input_manifest_ref: str):
    return resolve_method_env(
        output,
        stage="assembly_review",
        method_name=method_name,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=input_manifest_ref,
    )


def review_methods_error(methods: list[str]) -> str | None:
    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    missing = [method for method in methods if method not in registry.methods]
    return f"assembly_review descriptors missing: {missing}" if missing else None


def local_execution_enabled(output: dict[str, Any]) -> bool:
    return output.get("execution_mode") == "local"


def materialized_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [candidate for candidate in candidates if candidate.get("final_assembly_ref") and Path(candidate["final_assembly_ref"]).exists()]


def safe_candidate_id(candidate_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate_id).strip("_") or "candidate"


def run_attempts(
    state: TopLevelState,
    *,
    frozen_status: str,
    attempts_key: str,
    ran_status: str,
    results_key: str,
    method_name: str,
    fail_label: str,
) -> dict:
    output = review_output(state)
    if output.get("status") != frozen_status:
        return {}
    results = []
    for payload in output.get(attempts_key, []):
        attempt = load_frozen_attempt(
            state,
            payload,
            expected_stage="assembly_review",
            expected_method_name=method_name,
        )
        outcome = run_frozen_attempt(
            attempt,
            data_dir=state.get("data_dir", "data"),
            input_manifest_ref=state.get("input_manifest_ref", ""),
            fail_label=fail_label,
            cleanup_outputs=False,
        )
        results.append(
            persist_execution_result(
                state,
                stage="assembly_review",
                method_name=attempt.method_name,
                result=outcome.result,
                error=outcome.error,
            ).model_dump(mode="json")
        )
    return update_review_output(state, {"status": ran_status, results_key: results})


def update_signal(
    signals: list[dict[str, Any]], *, signal_name: str, method_name: str, candidate_id: str,
    status: str, metric_refs: list[str], notes: list[str], metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    updated = []
    matched = False
    for signal in signals:
        if signal.get("signal_name") == signal_name and signal.get("candidate_id") in {None, candidate_id}:
            updated.append({**signal, "candidate_id": candidate_id, "status": status, "metric_refs": metric_refs, "notes": notes, "metadata": metadata})
            matched = True
        else:
            updated.append(signal)
    if not matched:
        updated.append(AssemblyEvalSignal(
            signal_name=signal_name, method_name=method_name, candidate_id=candidate_id,
            status=status, metric_refs=metric_refs, notes=notes, metadata=metadata,
        ).model_dump(mode="json"))
    return updated


def mark_missing_signals(
    signals: list[dict[str, Any]], candidates: list[dict[str, Any]], *, signal_name: str,
    method_name: str, reason: str, materialized_ids: set[Any] | None = None,
) -> list[dict[str, Any]]:
    updated = list(signals)
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        if materialized_ids is not None and candidate_id in materialized_ids:
            continue
        updated = update_signal(
            updated, signal_name=signal_name, method_name=method_name, candidate_id=str(candidate_id),
            status="missing", metric_refs=[], notes=[reason, "execution_skipped"],
            metadata={"draft_assembly_ref": candidate.get("final_assembly_ref")},
        )
    return updated
def write_json_artifact(state: TopLevelState, filename: str, payload: dict[str, Any], *, artifact_type: str) -> str:
    data_dir = Path(state.get("data_dir", "data"))
    out_dir = stage_artifacts_dir(state, "assembly_review")
    out_dir.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_hash = sha256(canonical).hexdigest()[:16]
    logical = Path(filename)
    suffix = logical.suffix or ".json"
    path = out_dir / f"{logical.stem}.{content_hash}{suffix}"
    temporary = stage_work_dir(
        state,
        "assembly_review",
        "persistence",
        f".{filename}.{uuid4().hex}.tmp",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        publish_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    artifact = ArtifactMeta.from_file(path, stage="assembly_review", method_name=ASSEMBLY_REVIEW_CONTRACT_METHOD, artifact_type=artifact_type)
    append_artifact_records(data_dir, state["session_id"], [artifact])
    return str(path)


def rewrite_eval_bundle(state: TopLevelState, output: dict[str, Any], *, status: str) -> dict:
    previous_bundle = read_eval_bundle(output.get("eval_bundle_ref"))
    signal_payloads = candidate_eval_signals(previous_bundle, output.get("signals", []))
    eval_signals = [AssemblyEvalSignal.model_validate(signal) for signal in signal_payloads]
    review_plan = dict(output.get("review_plan") or {})
    review_plan["status"] = status
    review_plan["available_signals"] = sorted({signal.signal_name for signal in eval_signals if signal.status == "available"})
    review_plan["missing_signals"] = sorted({signal.signal_name for signal in eval_signals if signal.status in {"missing", "failed"}})
    eval_bundle = AssemblyEvalBundle(
        session_id=state["session_id"],
        candidate_ids=[candidate.get("candidate_id", "unknown") for candidate in output.get("candidates", [])],
        signals=eval_signals,
        available_inputs=previous_bundle.get("available_inputs", []),
        missing_inputs=previous_bundle.get("missing_inputs", []),
        notes=["thresholds_not_applied_without_human_approval"],
    )
    payload = eval_bundle.model_dump(mode="json")
    ref = write_json_artifact(
        state,
        f"assembly_eval_bundle.{status}.json",
        payload,
        artifact_type=ASSEMBLY_REVIEW_EVAL_BUNDLE,
    )
    return update_review_output(state, {**output, "status": status, "signals": [signal.model_dump(mode="json") for signal in eval_signals], "review_plan": review_plan, "eval_bundle_ref": ref})


def read_eval_bundle(ref: Any) -> dict[str, Any]:
    path = Path(str(ref)) if ref else None
    return json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}


def candidate_eval_signals(previous_bundle: dict[str, Any], review_signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    updated = {(signal.get("signal_name"), signal.get("candidate_id")): signal for signal in review_signals if signal.get("candidate_id") is not None}
    result = []
    for signal in previous_bundle.get("signals", []):
        result.append(updated.pop((signal.get("signal_name"), signal.get("candidate_id")), signal))
    result.extend(updated.values())
    return result
