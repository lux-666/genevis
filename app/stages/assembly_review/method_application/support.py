"""Shared state and execution helpers for assembly-review metric methods.

This module is deliberately independent of the parent stage nodes.  Metric
subgraphs own their command lifecycle; the parent graph only plans and routes
the review workflow.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.configuration import load_descriptor_registry
from app.control.workflow_plan import current_workflow_step
from app.execution.method_outcomes import MethodOutcome
from app.schemas.artifacts import (
    ASSEMBLY_REVIEW_EVAL_BUNDLE,
    ArtifactMeta,
    ArtifactRef,
    AssemblyEvalBundle,
    AssemblyEvalSignal,
)
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.assembly_review.state import (
    ASSEMBLY_EVAL_SIGNAL_CURSOR_ADAPTER,
    ASSEMBLY_REVIEW_CURSOR_ADAPTER,
    ASSEMBLY_REVIEW_PLAN_CURSOR_ADAPTER,
    AssemblyCandidateCursor,
    AssemblyEvalSignalCursor,
    AssemblyReviewCursor,
    SignalStatus,
    assembly_review_output,
    update_assembly_review_output,
)
from app.stages.base import absolute_data_dir, stage_artifacts_dir, stage_work_dir
from app.storage.artifacts import publish_artifact_record, record_registered_artifacts
from app.storage.assembly_candidates import (
    VerifiedExecutedCandidate,
    load_executed_candidate_bundle,
)

ASSEMBLY_REVIEW_CONTRACT_METHOD = "assembly_review.contract"


def review_output(state: TopLevelState) -> AssemblyReviewCursor:
    return assembly_review_output(state)


def update_review_output(
    state: TopLevelState, payload: Mapping[str, object]
) -> StateUpdate:
    return update_assembly_review_output(state, payload)


def review_methods_error(methods: list[str]) -> str | None:
    registry = load_descriptor_registry()
    missing = [method for method in methods if method not in registry.methods]
    return f"assembly_review descriptors missing: {missing}" if missing else None


def local_execution_enabled(output: AssemblyReviewCursor) -> bool:
    return output.get("execution_mode") == "local"


def planner_evaluator_outcome(
    state: TopLevelState,
    *,
    method_name: str,
) -> MethodOutcome | None:
    """Settle a selected evaluator that cannot enter a local method graph.

    Planner routing bypasses the evaluator subgraphs when local execution was
    not authorised.  This policy is deliberately used through the common
    method-outcome settlement node, rather than making the stage planner a
    second Ledger writer.
    """

    output = review_output(state)
    # A failed occurrence is settled by the terminal policy below.  The
    # planner boundary only owns the explicit no-local-execution bypass; it
    # must not first write ``skipped`` and then force the failure finalizer to
    # attempt a conflicting ``not_started`` disposition for the same method.
    if str(output.get("status") or "") == "failed":
        return None
    if not evaluator_selected(state, method_name) or local_execution_enabled(output):
        return None
    return MethodOutcome(
        method_name=method_name,
        disposition="skipped",
        reason="local_execution_not_authorized",
    )


def evaluator_method_outcome(
    state: TopLevelState,
    *,
    method_name: str,
    signal_name: str,
) -> MethodOutcome | None:
    """Reduce one evaluator's complete candidate batch to one disposition."""

    output = review_output(state)
    if not evaluator_selected(state, method_name):
        return None
    candidate_ids = {
        str(candidate.get("candidate_id"))
        for candidate in output.get("candidates", [])
        if candidate.get("candidate_id") is not None
    }
    signals = [
        signal
        for signal in output.get("signals", [])
        if signal.get("signal_name") == signal_name
        and signal.get("candidate_id") in candidate_ids
    ]
    by_candidate = {
        str(signal["candidate_id"]): signal
        for signal in signals
        if signal.get("candidate_id") is not None
    }
    if (
        candidate_ids
        and len(by_candidate) == len(candidate_ids)
        and all(signal.get("status") == "available" for signal in by_candidate.values())
    ):
        return MethodOutcome(method_name=method_name, disposition="completed")
    has_attempt = any(
        isinstance(attempt, Mapping) and attempt.get("method_id") == method_name
        for attempt in output.get("execution_attempts") or []
    )
    if any(signal.get("status") == "failed" for signal in by_candidate.values()):
        if not has_attempt:
            return MethodOutcome(
                method_name=method_name,
                disposition="not_started",
                reason=_evaluator_preflight_reason(
                    by_candidate.values(), signal_name=signal_name
                ),
            )
        return MethodOutcome(
            method_name=method_name,
            disposition="failed",
            reason=f"{signal_name}_candidate_evaluation_failed",
        )
    if (
        candidate_ids
        and len(by_candidate) == len(candidate_ids)
        and all(signal.get("status") == "missing" for signal in by_candidate.values())
    ):
        notes = {
            str(note)
            for signal in by_candidate.values()
            for note in (signal.get("notes") or [])
        }
        if "execution_not_started" in notes:
            return MethodOutcome(
                method_name=method_name,
                disposition="not_started",
                reason=_evaluator_preflight_reason(
                    by_candidate.values(), signal_name=signal_name
                ),
            )
        reason = f"{signal_name}_requires_materialized_draft_assembly"
        return MethodOutcome(
            method_name=method_name,
            disposition="skipped",
            reason=reason,
        )
    if str(output.get("status") or "") == "failed" and has_attempt:
        return MethodOutcome(
            method_name=method_name,
            disposition="failed",
            reason=str(output.get("reason") or f"{signal_name}_execution_failed"),
        )
    if not has_attempt:
        return MethodOutcome(
            method_name=method_name,
            disposition="not_started",
            reason=_evaluator_preflight_reason(
                by_candidate.values(), signal_name=signal_name
            ),
        )
    return MethodOutcome(
        method_name=method_name,
        disposition="failed",
        reason=f"{signal_name}_candidate_evaluation_incomplete",
    )


def _evaluator_preflight_reason(
    signals: Iterable[object],
    *,
    signal_name: str,
) -> str:
    """Return the durable preflight note without consulting the Ledger."""

    for signal in signals:
        if not isinstance(signal, Mapping):
            continue
        for note in signal.get("notes") or []:
            if note not in {"execution_not_started", "execution_skipped"}:
                return str(note)
    return f"{signal_name}_preflight_not_started"


def mark_evaluator_not_started(
    output: AssemblyReviewCursor,
    *,
    method_name: str,
    signal_name: str,
    reason: str,
) -> list[AssemblyEvalSignalCursor]:
    """Mark every planned candidate evaluator unavailable before execution."""

    signals = list(output.get("signals") or [])
    for candidate in output.get("candidates", []):
        signals = update_signal(
            signals,
            signal_name=signal_name,
            method_name=method_name,
            candidate_id=str(candidate.get("candidate_id") or "unknown"),
            status="missing",
            metric_refs=[],
            notes=[reason, "execution_not_started"],
            metadata={},
        )
    return signals


def evaluator_selected(state: TopLevelState, method_name: str) -> bool:
    """Use only the active frozen Review occurrence's evaluator roster."""
    step = current_workflow_step(state)
    if step is None or step["stage"] != "assembly_review":
        raise ValueError(
            "evaluator selection requires an active Assembly Review occurrence"
        )
    if "selected_method_names" not in step:
        raise ValueError(
            "Assembly Review occurrence is missing its frozen method roster"
        )
    return method_name in step["selected_method_names"]


def verified_review_candidate(
    state: TopLevelState,
    candidate: AssemblyCandidateCursor,
) -> VerifiedExecutedCandidate:
    """Resolve one review cursor through its immutable executed bundle."""

    verified = load_executed_candidate_bundle(
        absolute_data_dir(state),
        state["session_id"],
        ArtifactRef.model_validate(candidate["bundle_ref"]),
        expected_method_name=candidate["method_name"],
        expected_candidate_id=candidate["candidate_id"],
    )
    projected_primary = ArtifactRef.model_validate(candidate["primary_output_ref"])
    if projected_primary != verified.primary_output_ref:
        raise ValueError(
            "review candidate primary_output_ref does not match its executed bundle"
        )
    return verified


def safe_candidate_id(candidate_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate_id).strip("_") or "candidate"


def update_signal(
    signals: list[AssemblyEvalSignalCursor],
    *,
    signal_name: str,
    method_name: str,
    candidate_id: str,
    status: SignalStatus,
    metric_refs: list[str],
    notes: list[str],
    metadata: Mapping[str, object],
) -> list[AssemblyEvalSignalCursor]:
    updated: list[AssemblyEvalSignalCursor] = []
    matched = False
    for signal in signals:
        if signal.get("signal_name") == signal_name and signal.get("candidate_id") in {
            None,
            candidate_id,
        }:
            updated.append(
                ASSEMBLY_EVAL_SIGNAL_CURSOR_ADAPTER.validate_python(
                    {
                        **signal,
                        "candidate_id": candidate_id,
                        "status": status,
                        "metric_refs": metric_refs,
                        "notes": notes,
                        "metadata": dict(metadata),
                    }
                )
            )
            matched = True
        else:
            updated.append(signal)
    if not matched:
        signal_model = AssemblyEvalSignal(
            signal_name=signal_name,
            method_name=method_name,
            candidate_id=candidate_id,
            status=status,
            metric_refs=metric_refs,
            notes=notes,
            metadata=dict(metadata),
        )
        updated.append(
            ASSEMBLY_EVAL_SIGNAL_CURSOR_ADAPTER.validate_python(
                signal_model.model_dump(mode="json")
            )
        )
    return updated


def mark_missing_signals(
    signals: list[AssemblyEvalSignalCursor],
    candidates: list[AssemblyCandidateCursor],
    *,
    signal_name: str,
    method_name: str,
    reason: str,
    materialized_ids: set[str | None] | None = None,
) -> list[AssemblyEvalSignalCursor]:
    updated = list(signals)
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        if materialized_ids is not None and candidate_id in materialized_ids:
            continue
        updated = update_signal(
            updated,
            signal_name=signal_name,
            method_name=method_name,
            candidate_id=str(candidate_id),
            status="missing",
            metric_refs=[],
            notes=[reason, "execution_skipped"],
            metadata={"primary_output_ref": candidate.get("primary_output_ref")},
        )
    return updated


def write_json_artifact(
    state: TopLevelState, filename: str, payload: dict[str, Any], *, artifact_type: str
) -> str:
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
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    artifact = ArtifactMeta.from_file(
        temporary,
        stage="assembly_review",
        method_name=ASSEMBLY_REVIEW_CONTRACT_METHOD,
        artifact_type=artifact_type,
    )
    try:
        published = publish_artifact_record(
            data_dir,
            state["session_id"],
            artifact,
            destination=path,
        )
        record_registered_artifacts(
            data_dir,
            state["session_id"],
            [published.ref],
        )
    finally:
        temporary.unlink(missing_ok=True)
    return str(published.path)


def rewrite_eval_bundle(
    state: TopLevelState, output: Mapping[str, object], *, status: str
) -> StateUpdate:
    validated_output = ASSEMBLY_REVIEW_CURSOR_ADAPTER.validate_python(output)
    previous_bundle = read_eval_bundle(validated_output.get("eval_bundle_ref"))
    signal_payloads = candidate_eval_signals(
        previous_bundle, validated_output.get("signals", [])
    )
    eval_signals = [
        AssemblyEvalSignal.model_validate(signal) for signal in signal_payloads
    ]
    review_plan = dict(validated_output.get("review_plan") or {})
    review_plan["status"] = status
    review_plan["available_signals"] = sorted(
        {signal.signal_name for signal in eval_signals if signal.status == "available"}
    )
    review_plan["missing_signals"] = sorted(
        {
            signal.signal_name
            for signal in eval_signals
            if signal.status in {"missing", "failed"}
        }
    )
    eval_bundle = AssemblyEvalBundle(
        session_id=state["session_id"],
        candidate_ids=[
            candidate.get("candidate_id", "unknown")
            for candidate in validated_output.get("candidates", [])
        ],
        signals=eval_signals,
        available_inputs=previous_bundle.get("available_inputs", []),
        missing_inputs=previous_bundle.get("missing_inputs", []),
        notes=["quality_metrics_are_advisory"],
    )
    payload = eval_bundle.model_dump(mode="json")
    ref = write_json_artifact(
        state,
        f"assembly_eval_bundle.{status}.json",
        payload,
        artifact_type=ASSEMBLY_REVIEW_EVAL_BUNDLE,
    )
    serialized_plan = ASSEMBLY_REVIEW_PLAN_CURSOR_ADAPTER.validate_python(review_plan)
    serialized_signals = [
        ASSEMBLY_EVAL_SIGNAL_CURSOR_ADAPTER.validate_python(
            signal.model_dump(mode="json")
        )
        for signal in eval_signals
    ]
    return update_review_output(
        state,
        {
            **validated_output,
            "status": status,
            "signals": serialized_signals,
            "review_plan": serialized_plan,
            "eval_bundle_ref": ref,
        },
    )


def read_eval_bundle(ref: object) -> dict[str, Any]:
    path = Path(str(ref)) if ref else None
    return (
        json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
    )


def candidate_eval_signals(
    previous_bundle: dict[str, Any],
    review_signals: list[AssemblyEvalSignalCursor],
) -> list[dict[str, Any]]:
    updated = {
        (signal.get("signal_name"), signal.get("candidate_id")): signal
        for signal in review_signals
        if signal.get("candidate_id") is not None
    }
    result = []
    for signal in previous_bundle.get("signals", []):
        result.append(
            updated.pop((signal.get("signal_name"), signal.get("candidate_id")), signal)
        )
    result.extend(updated.values())
    return result
