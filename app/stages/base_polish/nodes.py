"""HiFi Racon correction with paired evidence and explicit manual adoption."""

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
from app.schemas.artifacts import BASE_POLISH_ASSEMBLY, ArtifactRef
from app.schemas.execution import InputDependency
from app.schemas.run_config import BasePolishRunConfig
from app.schemas.state import ActiveInterrupt, StateUpdate, TopLevelState
from app.stages.base import append_route_trace, stage_work_dir
from app.stages.base_polish.state import read_base_polish_stage_output as output
from app.stages.base_polish.state import update_base_polish_stage_output as update
from app.stages.base_polish.validation import compare_outputs, load_comparison
from app.stages.finishing.advisor import loop, work_parts
from app.stages.finishing.read_inputs import write_normalized_fasta
from app.stages.finishing.review import approved_proposal
from app.stages.read_resolution import RuntimeReadMember, resolve_long_read_members
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import assembly_resource_spec
from app.stages.sequences import fasta_records, read_sequences
from app.storage.artifacts import (
    PublishedArtifact,
    publish_or_recover_work_artifact,
    resolve_artifact_ref,
)
from app.storage.assembly_candidates import (
    VerifiedExecutedCandidate,
    load_executed_candidate_bundle,
    publish_derived_assembly_candidate,
)
from app.storage.ledger import Ledger
from app.storage.paths import DataLayoutError, SessionPaths

METHOD = "base_polish.racon"


def method_outcome(state: TopLevelState) -> MethodOutcome | None:
    cursor = output(state)
    if loop(state, "base_polish"):
        return None
    if cursor.get("comparison_ref"):
        refs = tuple(
            ArtifactRef.model_validate(dict(cursor)[key])
            for key in ("output_ref", "comparison_ref")
        )
        for ref in refs:
            resolve_artifact_ref(state["data_dir"], state["session_id"], ref)
        return MethodOutcome(METHOD, "completed", refs=refs)
    if cursor.get("status") in {"failed", "blocked_env", "missing_tool"}:
        started = any(
            item.get("status") in {"executed", "execution_failed"}
            for item in cursor.get("execution_attempts", [])
        )
        return MethodOutcome(
            METHOD,
            "failed" if started else "not_started",
            reason=str(cursor.get("reason") or "base polishing failed"),
        )
    return None


LIFECYCLE = MethodExecutionLifecycle(
    stage="base_polish",
    name="HiFi Racon",
    method_name=METHOD,
    frozen_status="polish_frozen",
    ran_status="polish_ran",
    outcome_policy=method_outcome,
)


def build_base_polish_graph() -> CompiledMethodGraph:
    return build_method_graph(LIFECYCLE, freeze_base_polish, collect_base_polish)


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
        raise ValueError("Base Polish source candidate changed")
    return candidate


def enter_base_polish(state: TopLevelState) -> StateUpdate:
    cursor = output(state)
    if cursor.get("status"):
        return {}
    try:
        step = current_workflow_step(state)
        if not step or not step["allow_local_execution"]:
            raise ValueError("Base Polish local execution is not authorized")
        candidate = source_candidate(state)
        fasta_records(candidate.primary_output_path)
        _hifi_members(state)
        return update(
            state,
            {
                "status": "ready",
                "source_candidate_ref": candidate.ref.model_dump(mode="json"),
            },
        )
    except (OSError, EOFError, ValueError) as exc:
        return update(state, {"status": "failed", "reason": str(exc)})


def _hifi_members(state: TopLevelState) -> list[RuntimeReadMember]:
    members = resolve_long_read_members(state)
    if not members or any(member.platform != "pacbio_hifi" for member in members):
        raise ValueError(
            "Base Polish first slice requires an exclusively pacbio_hifi read set"
        )
    if any(member.format not in {"fasta", "fastq"} for member in members):
        raise ValueError("Base Polish reads require explicit fasta/fastq format")
    return members


def _publish(
    state: TopLevelState, path: Path, kind: str, **metadata: Any
) -> PublishedArtifact:
    return publish_or_recover_work_artifact(
        state["data_dir"],
        state["session_id"],
        path,
        stage="base_polish",
        method_name=METHOD,
        artifact_type=kind,
        metadata=metadata,
    )


def freeze_base_polish(state: TopLevelState) -> StateUpdate:
    cursor = output(state)
    if cursor.get("status") != "ready":
        return {}
    try:
        if loop(state, "base_polish"):
            approved_proposal(state, "base_polish")
        candidate = source_candidate(state)
        members = _hifi_members(state)
        parameters = BasePolishRunConfig.model_validate(
            {
                key: dict(cursor)[key]
                for key in BasePolishRunConfig.model_fields
                if key in cursor
            }
        )
        env, _, blocked = resolve_method_env(
            dict(cursor),
            stage="base_polish",
            method_name=METHOD,
            workflow_step_id=current_workflow_step_id(
                state, expected_stage="base_polish"
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
        if loop(state, "base_polish"):
            for key in ("decision_ref", "recommendation_ref", "config_ref"):
                ref = ArtifactRef.model_validate(loop(state, "base_polish")[key])
                dependencies.append(
                    artifact_input_dependency(
                        state,
                        ref,
                        resolve_artifact_ref(
                            state["data_dir"], state["session_id"], ref
                        ),
                    )
                )
        verify_input_dependencies(state, dependencies)
        work = stage_work_dir(
            state, "base_polish", *work_parts(state, "base_polish"), "racon"
        )
        work.mkdir(parents=True, exist_ok=True)
        normalized = work / "hifi.fasta"
        write_normalized_fasta(normalized, members)
        verify_input_dependencies(state, dependencies)
        normalized_artifact = _publish(
            state,
            normalized,
            "base_polish.hifi_reads",
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
                    "route": "hifi_single_round_racon",
                },
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        input_record = _publish(state, record, "base_polish.inputs")
        dependencies.extend(
            [
                artifact_input_dependency(
                    state, normalized_artifact.ref, normalized_artifact.path
                ),
                artifact_input_dependency(state, input_record.ref, input_record.path),
            ]
        )
        source = work / "source.fasta"
        with source.open("w", encoding="ascii") as handle:
            for name, sequence in read_sequences(
                candidate.primary_output_path, "fasta"
            ):
                handle.write(f">{name}\n{sequence}\n")
        source_artifact = _publish(state, source, "base_polish.source")
        dependencies.append(
            artifact_input_dependency(state, source_artifact.ref, source_artifact.path)
        )
        script = work / "run.sh"
        script.write_text(
            "\n".join(
                [
                    "#!/bin/bash",
                    "set -euo pipefail",
                    "mkdir tool_run",
                    "cd tool_run",
                    f"ln -s {shlex.quote(str(source_artifact.path))} source.fasta",
                    f"ln -s {shlex.quote(str(normalized_artifact.path))} reads.fasta",
                    "minimap2 --version > minimap2.version",
                    "racon --version > racon.version",
                    "minimap2 -x map-hifi -c --eqx --secondary=no "
                    f"-t {parameters.threads} source.fasta reads.fasta > before.paf",
                    f"racon -u --no-trimming -t {parameters.threads} "
                    f"-w {parameters.window_length} "
                    f"-e {parameters.error_threshold} "
                    f"-m {parameters.match} -x {parameters.mismatch} "
                    f"-g {parameters.gap} "
                    "reads.fasta before.paf source.fasta > candidate.fasta",
                    "minimap2 -x map-hifi -c --eqx --secondary=no "
                    f"-t {parameters.threads} candidate.fasta reads.fasta > after.paf",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        attempt = build_frozen_attempt(
            attempt_id=(
                f"{state['session_id']}-{current_workflow_step_id(state)}-racon-"
                f"{loop(state, 'base_polish').get('attempt', 0)}"
            ),
            session_id=state["session_id"],
            stage="base_polish",
            method_name=METHOD,
            script_path=script,
            working_dir=work,
            expected_outputs=[
                work / "tool_run" / name
                for name in (
                    "candidate.fasta",
                    "before.paf",
                    "after.paf",
                    "minimap2.version",
                    "racon.version",
                )
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


def collect_base_polish(state: TopLevelState) -> StateUpdate:
    executions = load_method_executions(state, LIFECYCLE)
    if len(executions) != 1 or not executions[0].evidence.success:
        raise DataLayoutError("Base Polish requires one successful execution")
    execution = executions[0]
    try:
        candidate = source_candidate(state)
        attempt = execution.attempt
        record_ref = ArtifactRef.model_validate(attempt.metadata["input_record_ref"])
        if record_ref != ArtifactRef.model_validate(
            output(state).get("input_record_ref")
        ):
            raise ValueError("Base Polish input record differs from frozen attempt")
        record_path = resolve_artifact_ref(
            state["data_dir"], state["session_id"], record_ref
        )
        record = json.loads(record_path.read_text())
        if ArtifactRef.model_validate(record["source_candidate_ref"]) != candidate.ref:
            raise ValueError("Base Polish frozen source candidate mismatch")
        paths = SessionPaths.from_run_dir(state["data_dir"])
        available = []
        for path in attempt.expected_outputs:
            recovered_path = paths.workflow_step_artifact_path(
                current_workflow_step_id(state, expected_stage="base_polish"),
                "base_polish",
                "outputs",
                *path.relative_to(stage_work_dir(state, "base_polish")).parts,
            )
            available.append(path if path.is_file() else recovered_path)
        if len(available) != 5:
            raise ValueError("polishing output roster mismatch")
        versions = [path.read_text().strip() for path in available[3:]]
        if not all(versions):
            raise ValueError("missing tool version evidence")
        normalized_ref = ArtifactRef.model_validate(record["normalized_reads_ref"])
        normalized = resolve_artifact_ref(
            state["data_dir"], state["session_id"], normalized_ref
        )
        report = compare_outputs(
            candidate.primary_output_path,
            available[0],
            normalized,
            available[1],
            available[2],
        )
        metadata = {
            "dependency_refs": [
                record_ref.model_dump(mode="json"),
                execution.execution_ref.model_dump(mode="json"),
            ]
        }
        products = [
            _publish(state, path, kind, **metadata)
            for path, kind in zip(
                attempt.expected_outputs,
                (
                    BASE_POLISH_ASSEMBLY,
                    "base_polish.before_paf",
                    "base_polish.after_paf",
                    "base_polish.minimap2_version",
                    "base_polish.racon_version",
                ),
                strict=True,
            )
        ]
        polished = publish_derived_assembly_candidate(
            state["data_dir"],
            state["session_id"],
            workflow_step_id=current_workflow_step_id(state),
            source_candidate=candidate,
            primary_output_ref=products[0].ref,
            stage="base_polish",
            method_name=METHOD,
            action_identity=execution.execution_ref.artifact_id,
        )
        report.update(
            {
                "input_record_ref": record_ref.model_dump(mode="json"),
                "source_candidate_ref": candidate.ref.model_dump(mode="json"),
                "polished_candidate_ref": polished.ref.model_dump(mode="json"),
                "output_ref": products[0].ref.model_dump(mode="json"),
                "evidence_refs": [
                    item.ref.model_dump(mode="json") for item in products
                ],
                "versions": dict(zip(("minimap2", "racon"), versions, strict=True)),
                "alignment_settings": "-x map-hifi -c --eqx --secondary=no",
            }
        )
        report_path = attempt.working_dir / "comparison.json"
        report_path.write_text(
            json.dumps(report, sort_keys=True, indent=2), encoding="utf-8"
        )
        comparison = _publish(state, report_path, "base_polish.comparison", **metadata)
        Ledger(state["data_dir"]).record_attempt_succeeded(
            state["session_id"],
            attempt,
            artifacts=[
                ref.model_dump(mode="json")
                for ref in (
                    *[item.ref for item in products],
                    comparison.ref,
                    polished.ref,
                )
            ],
        )
        return update(
            state,
            {
                "status": "review_ready",
                "output_ref": products[0].ref.model_dump(mode="json"),
                "comparison_ref": comparison.ref.model_dump(mode="json"),
                "polished_candidate_ref": polished.ref.model_dump(mode="json"),
            },
        )
    except (OSError, EOFError, KeyError, ValueError) as exc:
        raise DataLayoutError(f"invalid Base Polish collection: {exc}") from exc


def open_adoption_gate(state: TopLevelState) -> StateUpdate:
    cursor = output(state)
    if cursor.get("status") != "review_ready":
        return {}
    refs = {
        key: dict(cursor)[key]
        for key in (
            "source_candidate_ref",
            "output_ref",
            "comparison_ref",
            "polished_candidate_ref",
        )
    }
    for raw_ref in refs.values():
        resolve_artifact_ref(
            state["data_dir"], state["session_id"], ArtifactRef.model_validate(raw_ref)
        )
    payload = ReviewPayload(
        stage="base_polish",
        gate_id="base_polish_adoption",
        reason="base_polishing_adoption_required",
        candidates=[refs],
        decisions_allowed=[
            "accept_candidate",
            "keep_prior_assembly",
            "stop_human_review",
        ],
        auto_decidable=False,
        report_refs={
            key: ArtifactRef.model_validate(refs[key]).path_ref
            for key in ("output_ref", "comparison_ref")
        },
        notes=[
            "Read agreement is in-sample, not independent QV. "
            "Keep the source if benefit is unclear."
        ],
    )
    persisted = persist_review_request(
        state,
        stage="base_polish",
        gate_id="base_polish_adoption",
        reason=payload.reason,
        payload=payload,
        method_name=METHOD,
    )
    interrupt = persisted.compact_interrupt(
        stage="base_polish",
        gate_id="base_polish_adoption",
        reason=payload.reason,
    )
    Ledger(state["data_dir"]).record_review_requested(
        state["session_id"],
        "base_polish",
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
    cursor = output(state)
    source = source_candidate(state)
    polished = load_executed_candidate_bundle(
        state["data_dir"],
        state["session_id"],
        ArtifactRef.model_validate(cursor["polished_candidate_ref"]),
    )
    load_comparison(
        state["data_dir"],
        state["session_id"],
        cursor["comparison_ref"],
        candidate_refs=[source.ref, polished.ref],
    )
    alternative = source if candidate.ref == polished.ref else polished
    metadata = {
        "comparison_candidate_bundle_ref": alternative.ref.model_dump(mode="json"),
        "comparison_primary_output_ref": alternative.primary_output_ref.model_dump(
            mode="json"
        ),
        "polish_comparison_ref": cursor["comparison_ref"],
        "candidate_bundle_ref": candidate.ref.model_dump(mode="json"),
        "primary_output_ref": candidate.primary_output_ref.model_dump(mode="json"),
    }
    return {
        **advance_workflow_step(
            state,
            "base_polish",
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
            "base_polish",
            metadata,
        ),
        "active_interrupt": None,
        "route_trace": append_route_trace(state, "base_polish_stage"),
    }


def fail_base_polish(state: TopLevelState) -> StateUpdate:
    settle_method_outcomes(state, stage="base_polish", outcomes=method_outcome(state))
    reason = output(state).get("reason") or "Base Polish failed"
    return {
        **fail_workflow_step(state, "base_polish", reason),
        "active_interrupt": None,
        "route_trace": append_route_trace(state, "base_polish_stage"),
    }
