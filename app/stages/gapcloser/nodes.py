"""HiFi-only TGS-GapCloser adapter using the shared method lifecycle."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, cast

from app.control.workflow_plan import (
    advance_workflow_step,
    assembly_delivery,
    current_workflow_step,
    current_workflow_step_id,
    fail_workflow_step,
)
from app.execution.env_dispatch import resolve_method_env
from app.execution.input_integrity import (
    artifact_input_dependency,
    asset_input_dependencies,
    verify_input_dependencies,
)
from app.execution.lifecycle import (
    CompiledMethodGraph,
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_outcomes import MethodOutcome, settle_method_outcomes
from app.execution.method_runner import build_frozen_attempt
from app.review.base import ReviewPayload
from app.review.persistence import persist_review_request
from app.schemas.artifacts import GAPCLOSER_ASSEMBLY, ArtifactRef
from app.schemas.execution import InputDependency
from app.schemas.run_config import GapCloserRunConfig
from app.schemas.state import ActiveInterrupt, StateUpdate, TopLevelState
from app.stages.base import append_route_trace, stage_work_dir
from app.stages.gapcloser.state import read_gapcloser_stage_output as output
from app.stages.gapcloser.state import update_gapcloser_stage_output as update
from app.stages.gapcloser.validation import (
    fasta_records,
    read_sequences,
    sequence_summary,
    validate_gap_outputs,
)
from app.stages.read_resolution import RuntimeReadMember, resolve_long_read_members
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import assembly_resource_spec
from app.storage.artifacts import (
    PublishedArtifact,
    publish_or_recover_work_artifact,
    resolve_artifact_ref,
)
from app.storage.assembly_candidates import (
    VerifiedExecutedCandidate,
    load_executed_candidate_bundle,
)
from app.storage.ledger import Ledger
from app.storage.paths import DataLayoutError, SessionPaths

METHOD = "gapcloser.tgs_gapcloser"


def method_outcome(state: TopLevelState) -> MethodOutcome | None:
    cursor = output(state)
    if cursor.get("validation_ref"):
        refs = tuple(
            ArtifactRef.model_validate(dict(cursor)[key])
            for key in ("output_ref", "details_ref", "validation_ref")
        )
        for ref in refs:
            resolve_artifact_ref(state["data_dir"], state["session_id"], ref)
        return MethodOutcome(METHOD, "completed", refs=refs)
    if cursor.get("status") == "no_gaps":
        return MethodOutcome(METHOD, "skipped", reason="no_internal_N_gaps")
    if cursor.get("status") in {"failed", "blocked_env", "missing_tool"}:
        started = any(
            item.get("status") in {"executed", "execution_failed"}
            for item in cursor.get("execution_attempts", [])
        )
        return MethodOutcome(
            METHOD,
            "failed" if started else "not_started",
            reason=str(cursor.get("reason") or "gap closing failed"),
        )
    return None


LIFECYCLE = MethodExecutionLifecycle(
    stage="gapcloser",
    name="TGS-GapCloser HiFi",
    method_name=METHOD,
    frozen_status="gap_frozen",
    ran_status="gap_ran",
    outcome_policy=method_outcome,
)


def build_gapcloser_graph() -> CompiledMethodGraph:
    return build_method_graph(LIFECYCLE, freeze_gapcloser, collect_gapcloser)


def source_candidate(state: TopLevelState) -> VerifiedExecutedCandidate:
    resource = resolve_stage_resource(state, resource_spec=assembly_resource_spec())
    candidate = load_executed_candidate_bundle(
        state["data_dir"],
        state["session_id"],
        ArtifactRef.model_validate(resource.metadata.get("candidate_bundle_ref")),
    )
    if resource.paths != [str(candidate.primary_output_path)] or (
        ArtifactRef.model_validate(resource.metadata.get("primary_output_ref"))
        != candidate.primary_output_ref
    ):
        raise ValueError("current assembly resource does not match its candidate")
    recorded = output(state).get("source_candidate_ref")
    if recorded and ArtifactRef.model_validate(recorded) != candidate.ref:
        raise ValueError("GapCloser source candidate changed")
    return candidate


def enter_gapcloser(state: TopLevelState) -> StateUpdate:
    cursor = output(state)
    if cursor.get("status"):
        return {}
    try:
        step = current_workflow_step(state)
        if not step or not step["allow_local_execution"]:
            raise ValueError("GapCloser local execution is not authorized")
        candidate = source_candidate(state)
        summary = sequence_summary(fasta_records(candidate.primary_output_path))
        status = "ready" if summary["internal_gaps"] else "no_gaps"
        if status == "ready":
            _hifi_members(state)
        return update(
            state,
            {
                "status": status,
                "source_candidate_ref": candidate.ref.model_dump(mode="json"),
            },
        )
    except (OSError, EOFError, ValueError) as exc:
        return update(state, {"status": "failed", "reason": str(exc)})


def _hifi_members(state: TopLevelState) -> list[RuntimeReadMember]:
    members = resolve_long_read_members(state)
    if not members or any(member.platform != "pacbio_hifi" for member in members):
        raise ValueError(
            "GapCloser first slice requires an exclusively pacbio_hifi read set"
        )
    if any(member.format not in {"fasta", "fastq"} for member in members):
        raise ValueError("GapCloser reads require explicit fasta/fastq format")
    return members


def _publish(
    state: TopLevelState, path: Path, kind: str, **metadata: Any
) -> PublishedArtifact:
    return publish_or_recover_work_artifact(
        state["data_dir"],
        state["session_id"],
        path,
        stage="gapcloser",
        method_name=METHOD,
        artifact_type=kind,
        metadata=metadata,
    )


def freeze_gapcloser(state: TopLevelState) -> StateUpdate:
    cursor = output(state)
    if cursor.get("status") != "ready":
        return {}
    try:
        candidate = source_candidate(state)
        members = _hifi_members(state)
        parameters = GapCloserRunConfig.model_validate(
            {
                key: dict(cursor)[key]
                for key in GapCloserRunConfig.model_fields
                if key in cursor
            }
        )
        env, _, blocked = resolve_method_env(
            dict(cursor),
            stage="gapcloser",
            method_name=METHOD,
            workflow_step_id=current_workflow_step_id(
                state, expected_stage="gapcloser"
            ),
            session_id=state["session_id"],
            data_dir=state["data_dir"],
            input_manifest_ref=state.get("input_manifest_ref", ""),
        )
        if blocked:
            return update(state, blocked)
        dependencies: list[InputDependency] = [
            artifact_input_dependency(
                state, candidate.primary_output_ref, candidate.primary_output_path
            )
        ]
        for member in members:
            if member.read_artifact_ref:
                dependencies.append(
                    artifact_input_dependency(
                        state, member.read_artifact_ref, member.path
                    )
                )
            else:
                dependencies.extend(
                    asset_input_dependencies(
                        state, [member.source_asset_id], [member.path]
                    )
                )
        verify_input_dependencies(state, dependencies)
        work = stage_work_dir(state, "gapcloser", "tgs_gapcloser")
        work.mkdir(parents=True, exist_ok=True)
        normalized = work / "hifi.fasta"
        with normalized.open("w", encoding="ascii") as handle:
            for index, member in enumerate(members):
                for ordinal, (_, sequence) in enumerate(
                    read_sequences(Path(member.path), member.format)
                ):
                    handle.write(f">member_{index}_read_{ordinal}\n{sequence}\n")
        verify_input_dependencies(state, dependencies)
        normalized_artifact = _publish(
            state,
            normalized,
            "gapcloser.hifi_reads",
            source_members=[member.model_dump(mode="json") for member in members],
        )
        record = work / "inputs.json"
        record.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_candidate_ref": candidate.ref.model_dump(mode="json"),
                    "assembly_ref": candidate.primary_output_ref.model_dump(
                        mode="json"
                    ),
                    "reads": [member.model_dump(mode="json") for member in members],
                    "normalized_reads_ref": normalized_artifact.ref.model_dump(
                        mode="json"
                    ),
                    "parameters": parameters.model_dump(mode="json"),
                    "route": "hifi_no_error_correction",
                },
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        input_record = _publish(state, record, "gapcloser.inputs")
        dependencies.extend(
            [
                artifact_input_dependency(
                    state, normalized_artifact.ref, normalized_artifact.path
                ),
                artifact_input_dependency(state, input_record.ref, input_record.path),
            ]
        )
        # Upstream shell expansions are not consistently quoted. Give the tool
        # simple relative names; our own quoted symlinks preserve source paths.
        # Its C++ reader dispatches gzip by suffix, unlike our magic-byte reader.
        with candidate.primary_output_path.open("rb") as handle:
            assembly_name = (
                "assembly.fasta.gz"
                if handle.read(2) == b"\x1f\x8b"
                else "assembly.fasta"
            )
        argv = [
            "tgsgapcloser",
            "--scaff",
            assembly_name,
            "--reads",
            "hifi.fasta",
            "--output",
            "result",
            "--ne",
            "--tgstype",
            "pb",
            "--minmap_arg",
            "-x asm20",
            "--thread",
            str(parameters.threads),
            "--min_idy",
            str(parameters.min_identity),
            "--min_match",
            str(parameters.min_match),
        ]
        if parameters.gap_size_check:
            argv.append("--g_check")
        script = work / "run.sh"
        script.write_text(
            "\n".join(
                [
                    "#!/bin/bash",
                    "set -euo pipefail",
                    # Never reuse upstream's tags after an uncertain process crash.
                    "mkdir tool_run",
                    "cd tool_run",
                    f"ln -s {shlex.quote(str(candidate.primary_output_path))} "
                    f"{assembly_name}",
                    f"ln -s {shlex.quote(str(normalized_artifact.path))} hifi.fasta",
                    shlex.join(argv),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        attempt = build_frozen_attempt(
            attempt_id=f"{state['session_id']}-{current_workflow_step_id(state)}-tgs",
            session_id=state["session_id"],
            stage="gapcloser",
            method_name=METHOD,
            script_path=script,
            working_dir=work,
            expected_outputs=[
                work / "tool_run" / "result.scaff_seqs",
                work / "tool_run" / "result.gap_fill_detail",
            ],
            env=cursor.get("env"),
            env_spec=env,
            input_dependencies=dependencies,
            metadata={"input_record_ref": input_record.ref.model_dump(mode="json")},
        )
        return freeze_method_attempts(
            state,
            LIFECYCLE,
            [attempt],
            stage_updates={
                "input_record_ref": input_record.ref.model_dump(mode="json"),
            },
        )
    except (OSError, EOFError, ValueError) as exc:
        return update(state, {"status": "failed", "reason": str(exc)})


def collect_gapcloser(state: TopLevelState) -> StateUpdate:
    executions = load_method_executions(state, LIFECYCLE)
    if len(executions) != 1 or not executions[0].evidence.success:
        raise DataLayoutError("GapCloser requires one successful execution")
    execution = executions[0]
    try:
        candidate = source_candidate(state)
        attempt = execution.attempt
        record_ref = ArtifactRef.model_validate(attempt.metadata["input_record_ref"])
        if record_ref != ArtifactRef.model_validate(
            output(state).get("input_record_ref")
        ):
            raise ValueError("GapCloser input record differs from frozen attempt")
        record_path = resolve_artifact_ref(
            state["data_dir"], state["session_id"], record_ref
        )
        record = json.loads(record_path.read_text())
        if ArtifactRef.model_validate(record["source_candidate_ref"]) != candidate.ref:
            raise ValueError("GapCloser frozen source candidate mismatch")
        paths = SessionPaths.from_run_dir(state["data_dir"])
        available = []
        for path in attempt.expected_outputs:
            recovered_path = paths.workflow_step_artifact_path(
                current_workflow_step_id(state, expected_stage="gapcloser"),
                "gapcloser",
                "outputs",
                *path.relative_to(stage_work_dir(state, "gapcloser")).parts,
            )
            available.append(path if path.is_file() else recovered_path)
        report = validate_gap_outputs(candidate.primary_output_path, *available)
        metadata = {
            "dependency_refs": [
                record_ref.model_dump(mode="json"),
                execution.execution_ref.model_dump(mode="json"),
            ]
        }
        fasta = _publish(
            state, attempt.expected_outputs[0], GAPCLOSER_ASSEMBLY, **metadata
        )
        details = _publish(
            state, attempt.expected_outputs[1], "gapcloser.details", **metadata
        )
        report.update(
            {
                "input_record_ref": record_ref.model_dump(mode="json"),
                "output_ref": fasta.ref.model_dump(mode="json"),
                "details_ref": details.ref.model_dump(mode="json"),
            }
        )
        report_path = attempt.working_dir / "validation.json"
        report_path.write_text(
            json.dumps(report, sort_keys=True, indent=2), encoding="utf-8"
        )
        validation = _publish(state, report_path, "gapcloser.validation", **metadata)
        Ledger(state["data_dir"]).record_attempt_succeeded(
            state["session_id"],
            attempt,
            artifacts=[
                item.ref.model_dump(mode="json")
                for item in (fasta, details, validation)
            ],
        )
        return update(
            state,
            {
                "status": "review_ready",
                "output_ref": fasta.ref.model_dump(mode="json"),
                "details_ref": details.ref.model_dump(mode="json"),
                "validation_ref": validation.ref.model_dump(mode="json"),
            },
        )
    except (OSError, EOFError, KeyError, ValueError) as exc:
        raise DataLayoutError(f"invalid GapCloser collection: {exc}") from exc


def open_adoption_gate(state: TopLevelState) -> StateUpdate:
    cursor = output(state)
    if cursor.get("status") != "review_ready":
        return {}
    refs = {
        key: dict(cursor)[key]
        for key in (
            "source_candidate_ref",
            "output_ref",
            "details_ref",
            "validation_ref",
        )
    }
    for raw_ref in refs.values():
        resolve_artifact_ref(
            state["data_dir"], state["session_id"], ArtifactRef.model_validate(raw_ref)
        )
    payload = ReviewPayload(
        stage="gapcloser",
        gate_id="gapcloser_adoption",
        reason="gap_closing_adoption_required",
        candidates=[refs],
        decisions_allowed=[
            "accept_candidate",
            "keep_prior_assembly",
            "stop_human_review",
        ],
        auto_decidable=False,
        report_refs={
            key: ArtifactRef.model_validate(refs[key]).path_ref
            for key in ("output_ref", "details_ref", "validation_ref")
        },
        notes=["Segment consistency is not a consensus-accuracy assessment."],
    )
    persisted = persist_review_request(
        state,
        stage="gapcloser",
        gate_id="gapcloser_adoption",
        reason=payload.reason,
        payload=payload,
        method_name=METHOD,
    )
    interrupt = persisted.compact_interrupt(
        stage="gapcloser",
        gate_id="gapcloser_adoption",
        reason=payload.reason,
    )
    Ledger(state["data_dir"]).record_review_requested(
        state["session_id"],
        "gapcloser",
        reason=payload.reason,
        candidates=payload.candidates,
        review_request_artifact_ref=persisted.request_ref.path_ref,
        review_request_id=persisted.review_request_id,
        auto_decidable=False,
    )
    return {
        **update(state, {"status": "blocked_human", "active_interrupt": interrupt}),
        "active_interrupt": cast(ActiveInterrupt, interrupt),
        "supervision_state": "blocked_human",
    }


def deliver_candidate(
    state: TopLevelState, candidate: VerifiedExecutedCandidate, *, materialized: bool
) -> StateUpdate:
    metadata = {
        "candidate_bundle_ref": candidate.ref.model_dump(mode="json"),
        "primary_output_ref": candidate.primary_output_ref.model_dump(mode="json"),
    }
    return {
        **advance_workflow_step(
            state,
            "gapcloser",
            outcome="completed",
            deliveries=(
                assembly_delivery(
                    metadata["candidate_bundle_ref"],
                    metadata["primary_output_ref"],
                    delivery_mode="materialized" if materialized else "reused",
                ),
            ),
        ),
        "resource_refs": update_resource_ref(
            state,
            "assembly",
            [str(candidate.primary_output_path)],
            "gapcloser",
            metadata,
        ),
        "active_interrupt": None,
        "route_trace": append_route_trace(state, "gapcloser_stage"),
    }


def finish_no_gaps(state: TopLevelState) -> StateUpdate:
    settle_method_outcomes(state, stage="gapcloser", outcomes=method_outcome(state))
    return {
        **deliver_candidate(state, source_candidate(state), materialized=False),
        **update(state, {"status": "completed", "reason": "no_internal_N_gaps"}),
    }


def fail_gapcloser(state: TopLevelState) -> StateUpdate:
    settle_method_outcomes(state, stage="gapcloser", outcomes=method_outcome(state))
    reason = output(state).get("reason") or "GapCloser failed"
    return {
        **fail_workflow_step(state, "gapcloser", reason),
        "active_interrupt": None,
        "route_trace": append_route_trace(state, "gapcloser_stage"),
    }
