"""Evidence, QUAST execution and durable proposals for the deduplication loop."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from app.control.workflow_plan import current_workflow_step_id
from app.execution.env_dispatch import resolve_method_env
from app.execution.evidence import load_execution_evidence
from app.execution.input_integrity import (
    artifact_input_dependency,
    asset_input_dependencies,
)
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_outcomes import MethodOutcome, has_execution_result
from app.execution.method_runner import build_frozen_attempt
from app.llm.dedup import PurgeParameters, PurgeRecommendation, PurgeRecommender
from app.review.automatic import completed_attempt_proposal
from app.review.base import ReviewPayload
from app.review.persistence import persist_review_request
from app.schemas.artifacts import ArtifactMeta, ArtifactRef, ArtifactRefJson
from app.schemas.execution import InputDependency
from app.schemas.state import ActiveInterrupt, StateUpdate, TopLevelState
from app.stages.assembly_dedup.nodes import (
    ASSEMBLY_DEDUP_METHOD,
    _dedup_source_candidate,
    _minimap2_preset,
    read_depth_members,
)
from app.stages.assembly_dedup.state import assembly_dedup_output as output
from app.stages.assembly_dedup.state import update_assembly_dedup_output as update
from app.stages.base import absolute_data_dir, stage_artifacts_dir, stage_work_dir
from app.stages.quast import validate_quast_comparison
from app.stages.resource_resolution import resolve_stage_resource
from app.stages.resource_specs import QUAST_REFERENCE_SPEC
from app.storage.artifacts import (
    publish_artifact_record,
    publish_or_recover_work_artifact,
    recover_published_artifact,
    resolve_artifact_ref,
)
from app.storage.assembly_candidates import load_executed_candidate_bundle
from app.storage.ledger import Ledger

STAGE = "assembly_dedup"
GATE = "dedup_recommendation"
QUAST = "assembly_dedup.eval.quast"


def load_json(state: TopLevelState, ref: Any) -> dict[str, Any]:
    path = resolve_artifact_ref(
        absolute_data_dir(state), state["session_id"], ArtifactRef.model_validate(ref)
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("dedup evidence must be a JSON object")
    return value


def publish_json(
    state: TopLevelState,
    name: str,
    payload: dict[str, Any],
    dependencies: Sequence[Mapping[str, Any]],
) -> ArtifactRef:
    source = stage_work_dir(state, STAGE, name)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        source,
        stage=STAGE,
        method_name=ASSEMBLY_DEDUP_METHOD,
        artifact_type=f"assembly_dedup.{Path(name).stem}",
        metadata={"dependency_refs": [dict(ref) for ref in dependencies]},
    )
    return publish_artifact_record(
        absolute_data_dir(state),
        state["session_id"],
        artifact,
        destination=stage_artifacts_dir(state, STAGE, name),
    ).ref


def history(state: TopLevelState) -> list[dict[str, Any]]:
    records = [load_json(state, ref) for ref in output(state).get("history_refs", [])]
    source = _dedup_source_candidate(state, output(state))
    for index, record in enumerate(records):
        if record["attempt"] != index or record["source_ref"] != source.ref.model_dump(
            mode="json"
        ):
            raise ValueError("dedup history source or attempt order mismatch")
        candidate = load_executed_candidate_bundle(
            absolute_data_dir(state),
            state["session_id"],
            ArtifactRef.model_validate(record["candidate_ref"]),
            expected_method_name=ASSEMBLY_DEDUP_METHOD,
            expected_parameter_attempt_id=index,
        )
        if record["primary_ref"] != candidate.primary_output_ref.model_dump(
            mode="json"
        ):
            raise ValueError("dedup history primary output mismatch")
        PurgeParameters.model_validate(record["parameters"])
    return records


def record_attempt(state: TopLevelState) -> StateUpdate:
    from app.stages.assembly_dedup.nodes import PURGE_DUPS_LIFECYCLE

    cursor = output(state)
    [execution] = load_method_executions(state, PURGE_DUPS_LIFECYCLE)
    source = _dedup_source_candidate(state, cursor)
    record: dict[str, Any] = {
        "attempt": cursor.get("current_attempt", 0),
        "source_ref": source.ref.model_dump(mode="json"),
        "candidate_ref": cursor["purged_candidate_bundle_ref"],
        "primary_ref": cursor["purged_primary_output_ref"],
        "execution_ref": execution.execution_ref.model_dump(mode="json"),
        "recommendation_ref": cursor["recommendation_ref"],
        "parameters": {
            "coverage_mode": cursor.get("coverage_mode", "read_depth"),
            "self_alignment_preset": cursor.get("self_alignment_preset", "asm5"),
            "end_only": cursor.get("get_seqs_exclude_haplotigs", True),
        },
        "command": execution.attempt.script_path.read_text(encoding="utf-8"),
        "return_code": execution.evidence.return_code,
    }
    ref = publish_json(
        state,
        f"attempts/{record['attempt']:03d}/assessment.json",
        record,
        [
            record["source_ref"],
            record["candidate_ref"],
            record["execution_ref"],
            record["recommendation_ref"],
        ],
    )
    refs = list(cursor.get("history_refs", []))
    if len(refs) != record["attempt"]:
        raise ValueError("dedup attempt history is not contiguous")
    refs.append(cast(ArtifactRefJson, ref.model_dump(mode="json")))
    return update(state, {"status": "evaluate_dedup", "history_refs": refs})


def quast_outcome(state: TopLevelState) -> MethodOutcome | None:
    cursor = output(state)
    if (
        cursor.get("recommendation_mode") != "advisor"
        or cursor.get("execution_mode") != "local"
        or not cursor.get("automatic_purge_approved")
    ):
        return None
    if cursor.get("status") not in {"dedup_selected", "pass_through", "failed"}:
        return None
    ledger = Ledger(absolute_data_dir(state))
    if has_execution_result(cursor, QUAST):
        executions = load_method_executions(state, QUAST_LIFECYCLE)
        if any(
            not execution.evidence.success
            or not ledger.has_attempt_success(
                state["session_id"], execution.attempt.attempt_id
            )
            for execution in executions
        ):
            return MethodOutcome(
                method_name=QUAST,
                disposition="failed",
                reason=str(cursor.get("reason") or "QUAST execution/collect failed"),
                refs=tuple(execution.execution_ref for execution in executions),
            )
    ref = cursor.get("quast_report_ref")
    execution_ref = cursor.get("quast_execution_ref")
    if ref and execution_ref:
        evidence = load_execution_evidence(
            state,
            execution_ref,
            expected_stage=STAGE,
            expected_method_name=QUAST,
        )
        report_ref = ArtifactRef.model_validate(ref)
        resolve_artifact_ref(absolute_data_dir(state), state["session_id"], report_ref)
        if evidence.success and ledger.has_attempt_success(
            state["session_id"], evidence.attempt_id
        ):
            return MethodOutcome(
                method_name=QUAST,
                disposition="completed",
                refs=(report_ref, ArtifactRef.model_validate(execution_ref)),
            )
    return MethodOutcome(
        method_name=QUAST,
        disposition="not_started",
        reason=cursor.get("reason", "quast_not_started"),
    )


QUAST_LIFECYCLE = MethodExecutionLifecycle(
    stage=STAGE,
    name="dedup QUAST comparison",
    method_name=QUAST,
    frozen_status="dedup_quast_frozen",
    ran_status="dedup_quast_ran",
    cleanup_outputs=False,
    outcome_policy=quast_outcome,
)


def build_quast_graph() -> Any:
    return build_method_graph(QUAST_LIFECYCLE, freeze_quast, collect_quast)


def freeze_quast(state: TopLevelState) -> StateUpdate:
    cursor = output(state)
    if cursor.get("status") != "evaluate_dedup":
        return {}
    env, _, blocked = resolve_method_env(
        dict(cursor),
        stage=STAGE,
        method_name=QUAST,
        workflow_step_id=current_workflow_step_id(state, expected_stage=STAGE),
        session_id=state["session_id"],
        data_dir=absolute_data_dir(state),
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked:
        return update(state, {**blocked, "status": "failed"})
    source = _dedup_source_candidate(state, cursor)
    records = history(state)
    refs = [
        source.primary_output_ref,
        *[ArtifactRef.model_validate(record["primary_ref"]) for record in records],
    ]
    paths = [
        resolve_artifact_ref(absolute_data_dir(state), state["session_id"], ref)
        for ref in refs
    ]
    dependencies: list[InputDependency] = [
        artifact_input_dependency(state, ref, path)
        for ref, path in zip(refs, paths, strict=True)
    ]
    reference = resolve_stage_resource(state, resource_spec=QUAST_REFERENCE_SPEC)
    reference_arg = ""
    if reference.found:
        if len(reference.paths) != 1 or len(reference.asset_records) != 1:
            raise ValueError("dedup QUAST requires one registered reference genome")
        dependencies.extend(
            asset_input_dependencies(
                state,
                [reference.asset_records[0].asset_id],
                reference.paths,
            )
        )
        reference_arg = f"-r {shlex.quote(reference.paths[0])} "
    work = stage_work_dir(state, STAGE, "evaluations", f"{len(records):03d}")
    work.mkdir(parents=True, exist_ok=True)
    report = work / "quast" / "report.tsv"
    labels = ",".join(["source", *[f"attempt_{i}" for i in range(len(records))]])
    script = work / "run_quast.sh"
    script.write_text(
        "set -euo pipefail\nquast.py "
        + " ".join(shlex.quote(str(path)) for path in paths)
        + f" {reference_arg}--labels {labels} --threads {cursor.get('threads', 8)}"
        + f" -o {shlex.quote(str(report.parent))}\n"
        + f"test -s {shlex.quote(str(report))}\n",
        encoding="utf-8",
    )
    attempt = build_frozen_attempt(
        attempt_id=f"{state['session_id']}:dedup:quast:{len(records)}",
        session_id=state["session_id"],
        stage=STAGE,
        method_name=QUAST,
        script_path=script,
        working_dir=work,
        expected_outputs=[report],
        env=cursor.get("env"),
        env_spec=env,
        input_dependencies=dependencies,
        metadata={"labels": labels.split(","), "reference_based": reference.found},
    )
    return freeze_method_attempts(state, QUAST_LIFECYCLE, [attempt])


def collect_quast(state: TopLevelState) -> StateUpdate:
    [execution] = load_method_executions(state, QUAST_LIFECYCLE)
    report = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        execution.attempt.expected_outputs[0],
        stage=STAGE,
        method_name=QUAST,
        artifact_type="assembly_dedup.quast_report",
        metadata={"dependency_refs": [execution.execution_ref.model_dump(mode="json")]},
    )
    validate_quast_comparison(report.path, execution.attempt.metadata["labels"])
    Ledger(absolute_data_dir(state)).record_attempt_succeeded(
        state["session_id"],
        execution.attempt,
        artifacts=[report.ref.model_dump(mode="json")],
    )
    return update(
        state,
        {
            "status": "dedup_advice_ready",
            "quast_report_ref": report.ref.model_dump(mode="json"),
            "quast_execution_ref": execution.execution_ref.model_dump(mode="json"),
        },
    )


def execution_capabilities(state: TopLevelState) -> dict[str, Any]:
    """Project the same input requirements used by the purge executor."""
    members = []
    reason = None
    presets = []
    try:
        members = read_depth_members(state, output(state))
        presets = [_minimap2_preset(member.platform) for member in members]
    except (OSError, TypeError, ValueError) as exc:
        reason = str(exc)
    return {
        "long_reads": {
            "available": bool(members),
            "members": [
                {"member_id": member.member_id, "platform": member.platform}
                for member in members
            ],
            "mapping_presets": presets,
        },
        "available_coverage_modes": ["self_alignment"]
        + (["read_depth"] if reason is None else []),
        "read_depth_unavailable_reason": reason,
    }


def validate_proposal(
    proposal: PurgeRecommendation,
    records: list[dict[str, Any]],
    maximum: int,
    *,
    available_coverage_modes: Sequence[str],
) -> None:
    if proposal.action == "run":
        if len(records) >= maximum:
            raise ValueError("dedup attempt budget exhausted")
        assert proposal.parameters is not None
        if proposal.parameters.coverage_mode not in available_coverage_modes:
            raise ValueError("dedup proposal selects an unavailable coverage_mode")
        if any(
            row["parameters"] == proposal.parameters.model_dump() for row in records
        ):
            raise ValueError("dedup proposal repeats an executed configuration")
    if proposal.action == "accept" and not any(
        row["attempt"] == proposal.selected_attempt for row in records
    ):
        raise ValueError("dedup proposal selects an unavailable attempt")


def recommend_dedup(state: TopLevelState) -> StateUpdate:
    cursor = output(state)
    if cursor.get("status") != "dedup_advice_ready":
        return {}
    records = history(state)
    round_id = len(records)
    source = _dedup_source_candidate(state, cursor)
    report_ref = ArtifactRef.model_validate(cursor["quast_report_ref"])
    report = resolve_artifact_ref(
        absolute_data_dir(state), state["session_id"], report_ref
    )
    recommender = PurgeRecommender(
        cursor.get("recommendation_output_method", "json_schema")
    )
    capabilities = execution_capabilities(state)
    evidence = {
        "execution_capabilities": capabilities,
        "max_attempts": cursor.get("max_attempts", 3),
        "remaining_attempts": max(0, cursor.get("max_attempts", 3) - round_id),
        "source_method": source.bundle.method_name,
        "genome_size_prior": source.bundle.genome_size_prior,
        "quast_report_tsv": report.read_text(encoding="utf-8"),
        "attempts": [
            {
                **{
                    key: row[key]
                    for key in ("attempt", "parameters", "command", "return_code")
                },
                "prior_recommendation": load_json(state, row["recommendation_ref"])[
                    "proposal"
                ],
            }
            for row in records
        ],
        "additional_quality_evidence": {"busco": None, "kmer_completeness": None},
    }
    dependencies = [
        source.ref.model_dump(mode="json"),
        report_ref.model_dump(mode="json"),
        *cursor.get("history_refs", []),
    ]
    if (cursor.get("review_policy") or {}).get("max_blocked_seconds") == 0:
        evidence["execution_policy"] = "run_valid_outputs"
    policy_proposal = (
        completed_attempt_proposal(
            completed_attempts=len(records),
            candidate_attempts=[r["attempt"] for r in records],
            maximum=cursor.get("max_attempts", 3),
        )
        if evidence.get("execution_policy") == "run_valid_outputs"
        else None
    )
    request = {
        "messages": [] if policy_proposal else recommender.messages(evidence),
        "evidence": evidence,
        "output_method": "policy" if policy_proposal else recommender.method,
        "dependency_refs": dependencies,
    }
    request_ref = publish_json(
        state, f"recommendations/{round_id:03d}/request.json", request, dependencies
    )
    name = f"recommendations/{round_id:03d}/response.json"
    metadata = {"dependency_refs": [request_ref.model_dump(mode="json")]}
    recovered = recover_published_artifact(
        absolute_data_dir(state),
        state["session_id"],
        stage_artifacts_dir(state, STAGE, name),
        stage=STAGE,
        method_name=ASSEMBLY_DEDUP_METHOD,
        artifact_type="assembly_dedup.response",
        metadata=metadata,
    )
    if recovered:
        response_ref = recovered.ref
        response = load_json(state, response_ref)
    else:
        if policy_proposal:
            response = {
                "request_ref": request_ref.model_dump(mode="json"),
                "proposal": policy_proposal,
                "error": None,
                "source": "policy",
            }
        else:
            result = recommender.invoke(request["messages"])
            response = {
                "request_ref": request_ref.model_dump(mode="json"),
                "raw_response": result.raw_response,
                "raw_message": result.raw_message,
                "model_id": result.model_id,
                "model_call": result.model_call.model_dump(mode="json"),
                "error": result.error,
                "proposal": result.parsed.model_dump(mode="json")
                if result.parsed
                else None,
            }
        response_ref = publish_json(state, name, response, metadata["dependency_refs"])
    if response["request_ref"] != request_ref.model_dump(mode="json"):
        raise ValueError("dedup recommendation request binding mismatch")
    proposal = None
    error = response.get("error")
    if response.get("proposal") is not None:
        try:
            proposal = PurgeRecommendation.model_validate(response["proposal"])
            validate_proposal(
                proposal,
                records,
                cursor.get("max_attempts", 3),
                available_coverage_modes=capabilities["available_coverage_modes"],
            )
        except ValueError as exc:
            proposal, error = None, str(exc)
    decisions = ["keep_prior_assembly", "stop_human_review"]
    if records:
        decisions.insert(0, "accept_candidate")
    if proposal and proposal.action != "human_review" and not policy_proposal:
        decisions.insert(0, "follow_recommendation")
    payload = ReviewPayload(
        stage=STAGE,
        gate_id=GATE,
        reason="dedup_recommendation_ready",
        decisions_allowed=decisions,
        auto_decidable=False,
        recommendation_error=error,
        candidates=[
            {
                "attempt": row["attempt"],
                "candidate_id": f"attempt_{row['attempt']}",
                "candidate_ref": row["candidate_ref"],
            }
            for row in records
        ],
        candidate_comparison_context={
            "recommendation_ref": response_ref.model_dump(mode="json"),
            "source_ref": source.ref.model_dump(mode="json"),
            "history_refs": cursor.get("history_refs", []),
            "quast_report_ref": report_ref.model_dump(mode="json"),
        },
        report_refs={
            "quast": report_ref.path_ref,
            "recommendation": response_ref.path_ref,
        },
        notes=[proposal.reason if proposal else str(error or "human review requested")],
    )
    persisted = persist_review_request(
        state,
        stage=STAGE,
        gate_id=GATE,
        reason=payload.reason,
        payload=payload,
        method_name=ASSEMBLY_DEDUP_METHOD,
    )
    interrupt = persisted.compact_interrupt(
        stage=STAGE, gate_id=GATE, reason=payload.reason
    )
    Ledger(absolute_data_dir(state)).record_review_requested(
        state["session_id"],
        STAGE,
        reason=payload.reason,
        review_request_artifact_ref=persisted.request_ref.path_ref,
        review_request_id=persisted.review_request_id,
        auto_decidable=False,
    )
    return {
        **update(
            state,
            {
                "status": "blocked_human",
                "recommendation_ref": response_ref.model_dump(mode="json"),
                "active_interrupt": interrupt,
            },
        ),
        "active_interrupt": cast(ActiveInterrupt, interrupt),
        "supervision_state": "blocked_human",
    }
