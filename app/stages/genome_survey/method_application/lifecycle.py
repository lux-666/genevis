"""Shared lifecycle helpers for executable genome-survey methods."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from app.storage.ledger import Ledger
from app.storage.artifacts import publish_artifact_records
from app.execution.evidence import (
    load_execution_evidence,
    load_frozen_attempt,
    persist_execution_result,
)
from app.execution.method_runner import run_frozen_attempt
from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState


def build_method_graph(
    freeze: Callable[[TopLevelState], dict],
    run: Callable[[TopLevelState], dict],
    collect: Callable[[TopLevelState], dict],
    *,
    frozen_status: str,
    ran_status: str,
):
    """Build the common freeze -> run -> collect method subgraph."""
    graph = StateGraph(TopLevelState)
    graph.add_node("freeze", freeze)
    graph.add_node("run", run)
    graph.add_node("collect", collect)
    graph.add_node("done", lambda _state: {})
    graph.add_edge(START, "freeze")
    graph.add_conditional_edges(
        "freeze",
        lambda state: "run" if _status(state) == frozen_status else "done",
        {"run": "run", "done": "done"},
    )
    graph.add_conditional_edges(
        "run",
        lambda state: "collect" if _status(state) == ran_status else "done",
        {"collect": "collect", "done": "done"},
    )
    graph.add_edge("collect", "done")
    graph.add_edge("done", END)
    return graph.compile()


def _status(state: TopLevelState) -> str | None:
    return genome_survey_output(state).get("status")


def genome_survey_output(state: TopLevelState) -> dict[str, Any]:
    """Return a shallow copy of the genome-survey stage output."""
    return dict((state.get("stage_outputs") or {}).get("genome_survey", {}))


def update_genome_survey_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    """Merge a method update into the genome-survey stage output."""
    outputs = dict(state.get("stage_outputs") or {})
    outputs["genome_survey"] = {**genome_survey_output(state), **payload}
    return {"stage_outputs": outputs}


def run_method_attempt(
    state: TopLevelState,
    *,
    frozen_status: str,
    frozen_attempt_key: str,
    execution_result_key: str,
    ran_status: str,
) -> dict:
    """Run one frozen genome-survey method attempt and persist its outcome."""
    output = genome_survey_output(state)
    if output.get("status") != frozen_status:
        return {}

    attempt = load_frozen_attempt(
        state,
        output[frozen_attempt_key],
        expected_stage="genome_survey",
    )
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label=attempt.method_name,
    )
    result_ref = persist_execution_result(
        state,
        stage="genome_survey",
        method_name=attempt.method_name,
        result=outcome.result,
        error=outcome.error,
    ).model_dump(mode="json")
    if not outcome.success:
        return update_genome_survey_output(
            state,
            {"status": "failed", "reason": outcome.error, execution_result_key: result_ref},
        )
    return update_genome_survey_output(
        state,
        {"status": ran_status, execution_result_key: result_ref},
    )


def attempt_artifacts(
    attempt: FrozenAttemptSpec,
    artifact_types_by_path: dict[str, str],
) -> list[ArtifactMeta]:
    """Build artifact metadata for expected biological outputs."""
    artifacts: list[ArtifactMeta] = []
    for path in attempt.expected_outputs:
        if not path.exists():
            raise FileNotFoundError(f"expected output missing: {path}")
        artifacts.append(
            ArtifactMeta.from_file(
                path,
                stage="genome_survey",
                method_name=attempt.method_name,
                artifact_type=artifact_types_by_path[str(path)],
                metadata={"attempt_id": attempt.attempt_id},
            )
        )
    return artifacts


def validate_attempt_execution(
    state: TopLevelState,
    attempt: FrozenAttemptSpec,
    value: dict[str, Any],
) -> None:
    """Fail closed unless a checkpoint ref matches this frozen attempt."""

    load_execution_evidence(
        state,
        value,
        expected_stage="genome_survey",
        expected_attempt_id=attempt.attempt_id,
        expected_method_name=attempt.method_name,
    )


def record_attempt_success(
    state: TopLevelState,
    attempt: FrozenAttemptSpec,
    artifacts: list[ArtifactMeta],
) -> None:
    """Register method artifacts and append successful-attempt ledger events."""
    data_dir = Path(state.get("data_dir", "data"))
    publish_artifact_records(data_dir, attempt.session_id, artifacts)
    ledger = Ledger(data_dir)
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(attempt.session_id, attempt, artifacts=artifact_payloads)
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(
            attempt.session_id,
            artifact,
            attempt_id=attempt.attempt_id,
        )
