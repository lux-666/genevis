"""Shared state and execution helpers for assembly-review metric methods.

This module is deliberately independent of the parent stage nodes.  Metric
subgraphs own their command lifecycle; the parent graph only plans and routes
the review workflow.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.configuration import load_descriptor_registry
from app.schemas.artifacts import (
    ASSEMBLY_REVIEW_EVAL_BUNDLE,
    ArtifactMeta,
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
from app.stages.base import stage_artifacts_dir, stage_work_dir
from app.storage.artifacts import publish_artifact_record

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


def materialized_candidates(
    candidates: list[AssemblyCandidateCursor],
) -> list[AssemblyCandidateCursor]:
    materialized: list[AssemblyCandidateCursor] = []
    for candidate in candidates:
        final_ref = candidate.get("final_assembly_ref")
        if final_ref and Path(final_ref).exists():
            materialized.append(candidate)
    return materialized


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
            metadata={"draft_assembly_ref": candidate.get("final_assembly_ref")},
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
        notes=["thresholds_not_applied_without_human_approval"],
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
