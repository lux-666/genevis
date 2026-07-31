"""Assembly deduplication stage nodes."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from app.configuration import load_descriptor_registry
from app.execution.env_dispatch import resolve_method_env
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
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ArtifactMeta,
    ArtifactRef,
    AssemblyCandidateBundle,
)
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.assembly_dedup.state import (
    AssemblyDedupCursor,
    assembly_dedup_output,
    update_assembly_dedup_output,
)
from app.stages.base import (
    absolute_data_dir,
    append_route_trace,
    stage_artifacts_dir,
    stage_work_dir,
)
from app.stages.read_resolution import RuntimeReadMember, resolve_long_read_members
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import ASSEMBLY_CANDIDATES_SPEC
from app.storage.artifacts import (
    publish_artifact_record,
    register_artifact_records,
)
from app.storage.assembly_candidates import (
    VerifiedExecutedCandidate,
    load_executed_candidate_bundle,
)
from app.storage.ledger import Ledger
from app.storage.publish import shell_publish_file
from app.workflow import next_stage_name

ASSEMBLY_DEDUP_METHOD = "assembly_dedup.purge_dups"
PURGE_DUPS_LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly_dedup",
    name="assembly_dedup purge_dups",
    method_name=ASSEMBLY_DEDUP_METHOD,
    frozen_status="dedup_frozen",
    ran_status="dedup_ran",
    cleanup_outputs=False,
)


def build_purge_dups_graph() -> CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]:
    """Build the shared purge_dups execution lifecycle."""

    return build_method_graph(
        PURGE_DUPS_LIFECYCLE,
        freeze_purge_dups_attempt,
        collect_purge_dups_attempt,
    )


def _output(state: TopLevelState) -> AssemblyDedupCursor:
    return assembly_dedup_output(state)


def _update_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    return update_assembly_dedup_output(state, payload)


def _fail(state: TopLevelState, reason: str) -> StateUpdate:
    return _update_output(state, {"status": "failed", "reason": reason})


def enter_assembly_dedup(state: TopLevelState) -> StateUpdate:
    if _output(state).get("status"):
        return {}
    return {"supervision_state": "running_stage"}


def plan_purge_dups(state: TopLevelState) -> StateUpdate:
    if _output(state).get("execution_mode") != "local":
        return _update_output(
            state,
            {
                "status": "skipped",
                "skip_reason": "local_execution_not_enabled",
            },
        )
    try:
        candidate = _selected_assembly_candidate(state)
    except (OSError, TypeError, ValueError) as exc:
        return _fail(state, f"assembly candidate handoff is invalid: {exc}")
    if candidate is None:
        return _update_output(
            state,
            {
                "status": "skipped",
                "skip_reason": "no_materialized_draft_assembly",
            },
        )

    dedup_dir = stage_artifacts_dir(state, "assembly_dedup")
    dedup_dir.mkdir(parents=True, exist_ok=True)
    return _update_output(
        state,
        {
            "status": "ready_to_run",
            "candidate_bundle_ref": candidate.ref.model_dump(mode="json"),
            "candidate_id": candidate.bundle.candidate_id,
            "input_primary_output_ref": (
                candidate.primary_output_ref.model_dump(mode="json")
            ),
            "method_name": ASSEMBLY_DEDUP_METHOD,
            "input_assembly_ref": str(candidate.primary_output_path),
            "output_assembly_ref": str(dedup_dir / "purged_assembly.fasta"),
            "input_member_ids": list(
                candidate.bundle.input_member_ids["primary"]
            ),
            "input_member_platforms": list(
                candidate.bundle.input_member_platforms["primary"]
            ),
            "cutoffs_strategy": "calcuts_auto",
            "get_seqs_exclude_haplotigs": True,
        },
    )


def freeze_purge_dups_attempt(state: TopLevelState) -> StateUpdate:
    dedup_out = _output(state)
    if dedup_out.get("status") != "ready_to_run":
        return {}

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    registry = load_descriptor_registry()
    if ASSEMBLY_DEDUP_METHOD not in registry.methods:
        return _fail(
            state, f"assembly dedup descriptor missing: {ASSEMBLY_DEDUP_METHOD}"
        )

    env_spec, _packages, blocked = resolve_method_env(
        dict(dedup_out),
        stage="assembly_dedup",
        method_name=ASSEMBLY_DEDUP_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_output(state, blocked)

    try:
        source_candidate = _dedup_source_candidate(state, dedup_out)
        selected_members = _selected_primary_members(
            state,
            list(dedup_out.get("input_member_ids") or []),
            list(dedup_out.get("input_member_platforms") or []),
        )
    except (OSError, TypeError, ValueError) as exc:
        return _fail(state, f"purge_dups frozen input is invalid: {exc}")

    input_assembly_ref = source_candidate.primary_output_path
    output_assembly_ref = Path(dedup_out["output_assembly_ref"])
    work_dir = stage_work_dir(state, "assembly_dedup")
    artifact_dir = stage_artifacts_dir(state, "assembly_dedup")
    work_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    script_path = work_dir / "run_purge_dups.sh"
    read_bam = artifact_dir / "reads_to_raw_assembly.bam"
    read_paf = artifact_dir / "reads_to_raw_assembly.paf.gz"
    split_assembly = artifact_dir / "raw_assembly.split.fasta"
    base_cov = artifact_dir / "PB.base.cov"
    pb_stat = artifact_dir / "PB.stat"
    cutoffs = artifact_dir / "cutoffs"
    self_paf = artifact_dir / "self.paf.gz"
    dups_bed = artifact_dir / "dups.bed"
    get_seqs_prefix = work_dir / "purged_assembly"
    get_seqs_primary = Path(f"{get_seqs_prefix}.purged.fa")
    member_bams = [
        work_dir / f"primary_member_{index:03d}.bam"
        for index in range(len(selected_members))
    ]
    member_pafs = [
        work_dir / f"primary_member_{index:03d}.paf.gz"
        for index in range(len(selected_members))
    ]
    mapping_presets = [
        _minimap2_preset(member.platform) for member in selected_members
    ]
    mapping_commands: list[str] = []
    for member, preset, member_bam, member_paf in zip(
        selected_members,
        mapping_presets,
        member_bams,
        member_pafs,
        strict=True,
    ):
        mapping_commands.extend(
            [
                (
                    f"minimap2 -t 8 -ax {shlex.quote(preset)} "
                    f"{shlex.quote(str(input_assembly_ref))} "
                    f"{shlex.quote(member.path)} "
                    f"| samtools sort -@ 8 -o {shlex.quote(str(member_bam))}"
                ),
                (
                    f"minimap2 -t 8 -x {shlex.quote(preset)} "
                    f"{shlex.quote(str(input_assembly_ref))} "
                    f"{shlex.quote(member.path)} "
                    f"| gzip -c > {shlex.quote(str(member_paf))}"
                ),
            ]
        )
    if len(selected_members) == 1:
        mapping_commands.extend(
            [
                shell_publish_file(member_bams[0], read_bam),
                shell_publish_file(member_pafs[0], read_paf),
            ]
        )
    else:
        merged_bam = work_dir / "reads_to_raw_assembly.merged.bam"
        merged_paf = work_dir / "reads_to_raw_assembly.merged.paf.gz"
        mapping_commands.extend(
            [
                (
                    f"samtools merge -f -@ 8 {shlex.quote(str(merged_bam))} "
                    + " ".join(shlex.quote(str(path)) for path in member_bams)
                ),
                (
                    "cat "
                    + " ".join(shlex.quote(str(path)) for path in member_pafs)
                    + f" > {shlex.quote(str(merged_paf))}"
                ),
                shell_publish_file(merged_bam, read_bam),
                shell_publish_file(merged_paf, read_paf),
            ]
        )
    script_path.write_text(
        "set -euo pipefail\n"
        + "\n".join(mapping_commands)
        + "\n"
        f"samtools index {shlex.quote(str(read_bam))}\n"
        f"cd {shlex.quote(str(artifact_dir))}\n"
        f"pbcstat {shlex.quote(str(read_paf))}\n"
        "test -s PB.base.cov && test -s PB.stat\n"
        f"calcuts {shlex.quote(str(pb_stat))} > {shlex.quote(str(cutoffs))}\n"
        f"split_fa {shlex.quote(str(input_assembly_ref))} > "
        f"{shlex.quote(str(split_assembly))}\n"
        f"test -s {shlex.quote(str(split_assembly))}\n"
        f"minimap2 -t 8 -x asm5 -DP {shlex.quote(str(split_assembly))} "
        f"{shlex.quote(str(split_assembly))} "
        f"| gzip -c > {shlex.quote(str(self_paf))}\n"
        f"purge_dups -2 -T {shlex.quote(str(cutoffs))} -c {shlex.quote(str(base_cov))} "
        f"{shlex.quote(str(self_paf))} > {shlex.quote(str(dups_bed))}\n"
        f"get_seqs -e -p {shlex.quote(str(get_seqs_prefix))} "
        f"{shlex.quote(str(dups_bed))} "
        f"{shlex.quote(str(input_assembly_ref))}\n"
        f"{shell_publish_file(get_seqs_primary, output_assembly_ref)}\n"
        f"test -s {shlex.quote(str(output_assembly_ref))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    input_dependencies = [
        artifact_input_dependency(
            state,
            source_candidate.primary_output_ref,
            input_assembly_ref,
        )
    ]
    for member in selected_members:
        if member.read_artifact_ref is not None:
            input_dependencies.append(
                artifact_input_dependency(
                    state,
                    member.read_artifact_ref,
                    member.path,
                )
            )
        else:
            input_dependencies.extend(
                asset_input_dependencies(
                    state,
                    [member.member_id],
                    [member.path],
                )
            )

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:assembly_dedup:purge_dups",
        session_id=session_id,
        stage="assembly_dedup",
        method_name=ASSEMBLY_DEDUP_METHOD,
        script_path=script_path,
        working_dir=work_dir,
        expected_outputs=[
            read_bam,
            read_paf,
            split_assembly,
            base_cov,
            pb_stat,
            cutoffs,
            self_paf,
            dups_bed,
            output_assembly_ref,
        ],
        env=dedup_out.get("env"),
        env_spec=env_spec,
        input_dependencies=input_dependencies,
        metadata={
            "candidate_bundle_ref": source_candidate.ref.model_dump(mode="json"),
            "input_primary_output_ref": (
                source_candidate.primary_output_ref.model_dump(mode="json")
            ),
            "candidate_id": dedup_out.get("candidate_id"),
            "input_assembly_ref": str(input_assembly_ref),
            "output_assembly_ref": str(output_assembly_ref),
            "input_member_ids": [member.member_id for member in selected_members],
            "input_member_platforms": [
                member.platform for member in selected_members
            ],
            "read_mapping_presets": mapping_presets,
            "read_bam_ref": str(read_bam),
            "read_paf_ref": str(read_paf),
            "split_assembly_ref": str(split_assembly),
            "base_cov_ref": str(base_cov),
            "pb_stat_ref": str(pb_stat),
            "cutoffs_ref": str(cutoffs),
            "self_paf_ref": str(self_paf),
            "dups_bed_ref": str(dups_bed),
        },
    )
    return freeze_method_attempts(state, PURGE_DUPS_LIFECYCLE, [attempt])


def collect_purge_dups_attempt(state: TopLevelState) -> StateUpdate:
    """Validate the completed purge_dups attempt before stage commit."""

    executions = load_method_executions(state, PURGE_DUPS_LIFECYCLE)
    if len(executions) != 1:
        return _fail(state, "purge_dups requires exactly one execution attempt")
    return _update_output(state, {"status": "dedup_ran"})


def commit_assembly_dedup(state: TopLevelState) -> StateUpdate:
    dedup_out = _output(state)
    if dedup_out.get("status") not in {"dedup_ran", "skipped"}:
        return {}

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    source_candidate = None
    if dedup_out.get("candidate_bundle_ref") is not None:
        try:
            source_candidate = _dedup_source_candidate(state, dedup_out)
        except (OSError, TypeError, ValueError) as exc:
            return _fail(state, f"dedup source candidate is invalid: {exc}")

    if dedup_out.get("status") == "skipped":
        stage_update = _update_output(
            state,
            {
                "status": "completed",
                "purged_assembly_ref": None,
                "purged_candidate_bundle_ref": None,
                "purged_primary_output_ref": None,
                "artifact_refs": [],
            },
        )
        update: StateUpdate = {
            "current_stage": next_stage_name("assembly_dedup"),
            "supervision_state": "running_stage",
            "route_trace": append_route_trace(state, "assembly_dedup_stage"),
            **stage_update,
        }
        if source_candidate is not None:
            update["resource_refs"] = update_resource_ref(
                state,
                "assembly",
                [str(source_candidate.primary_output_path)],
                "assembly_dedup",
                {
                    "candidate_bundle_ref": source_candidate.ref.model_dump(
                        mode="json"
                    ),
                    "primary_output_ref": (
                        source_candidate.primary_output_ref.model_dump(mode="json")
                    ),
                    "raw_candidate_bundle_ref": source_candidate.ref.model_dump(
                        mode="json"
                    ),
                    "raw_primary_output_ref": (
                        source_candidate.primary_output_ref.model_dump(mode="json")
                    ),
                    "deduplicated": False,
                },
            )
        return update

    if source_candidate is None:
        return _fail(state, "purge_dups has no verified source candidate")
    executions = load_method_executions(state, PURGE_DUPS_LIFECYCLE)
    if len(executions) != 1:
        return _fail(state, "purge_dups requires exactly one execution attempt")
    execution = executions[0]
    attempt = execution.attempt
    output_assembly_ref = Path(str(dedup_out["output_assembly_ref"]))
    if not output_assembly_ref.is_file() or output_assembly_ref.stat().st_size <= 0:
        return _fail(state, "purge_dups primary assembly output is missing or empty")

    artifacts: list[ArtifactMeta] = []
    artifacts.append(
        ArtifactMeta.from_file(
            output_assembly_ref,
            stage="assembly_dedup",
            method_name=ASSEMBLY_DEDUP_METHOD,
            artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
            metadata={
                "candidate_id": dedup_out.get("candidate_id"),
                "dependency_refs": [
                    source_candidate.primary_output_ref.model_dump(mode="json")
                ],
            },
        )
    )
    attempt_metadata = dict(attempt.metadata)
    for key, artifact_type in [
        ("read_bam_ref", "assembly_dedup.read_alignment_bam"),
        ("read_paf_ref", "assembly_dedup.read_alignment_paf"),
        ("split_assembly_ref", "assembly_dedup.split_assembly"),
        ("base_cov_ref", "assembly_dedup.base_coverage"),
        ("pb_stat_ref", "assembly_dedup.pb_stat"),
        ("cutoffs_ref", "assembly_dedup.cutoffs"),
        ("self_paf_ref", "assembly_dedup.self_paf"),
        ("dups_bed_ref", "assembly_dedup.dups_bed"),
    ]:
        path_ref = attempt_metadata.get(key)
        if path_ref and Path(str(path_ref)).exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    Path(str(path_ref)),
                    stage="assembly_dedup",
                    method_name=ASSEMBLY_DEDUP_METHOD,
                    artifact_type=artifact_type,
                    metadata={
                        "candidate_id": dedup_out.get("candidate_id"),
                        "dependency_refs": [
                            source_candidate.primary_output_ref.model_dump(
                                mode="json"
                            )
                        ],
                    },
                )
            )
    output_artifact_refs = register_artifact_records(
        data_dir,
        session_id,
        artifacts,
    )
    artifact_ref_by_path = {
        Path(artifact.file_path).resolve(strict=False): ref
        for artifact, ref in zip(artifacts, output_artifact_refs, strict=True)
    }
    primary_output = artifact_ref_by_path[output_assembly_ref.resolve(strict=False)]
    candidate_id = (
        f"{source_candidate.bundle.candidate_id}:{ASSEMBLY_DEDUP_METHOD}"
    )
    planned_candidate = AssemblyCandidateBundle(
        session_id=session_id,
        candidate_id=candidate_id,
        method_name=ASSEMBLY_DEDUP_METHOD,
        status="planned",
        input_type=source_candidate.bundle.input_type,
        input_member_ids={
            "primary": list(dedup_out.get("input_member_ids") or [])
        },
        input_member_platforms={
            "primary": list(dedup_out.get("input_member_platforms") or [])
        },
        parameter_attempt_id=0,
        genome_size_prior=source_candidate.bundle.genome_size_prior,
        expected_outputs=["primary_assembly"],
    )
    plan_path = output_assembly_ref.with_name("purged_candidate.planned.json")
    plan_source = stage_work_dir(
        state,
        "assembly_dedup",
        "purged_candidate.planned.json",
    )
    plan_source.write_text(
        json.dumps(planned_candidate.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    plan_artifact = ArtifactMeta.from_file(
        plan_source,
        stage="assembly_dedup",
        method_name=ASSEMBLY_DEDUP_METHOD,
        artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        metadata={
            "candidate_id": candidate_id,
            "dependency_refs": [
                source_candidate.ref.model_dump(mode="json"),
                source_candidate.primary_output_ref.model_dump(mode="json"),
            ],
        },
    )
    published_plan = publish_artifact_record(
        data_dir,
        session_id,
        plan_artifact,
        destination=plan_path,
    )
    executed_data = planned_candidate.model_dump(mode="json")
    executed_data.update(
        {
            "status": "executed",
            "plan_ref": published_plan.ref.model_dump(mode="json"),
            "primary_output": primary_output.model_dump(mode="json"),
            "log_refs": {
                "stdout": execution.evidence.stdout_ref.model_dump(mode="json"),
                "stderr": execution.evidence.stderr_ref.model_dump(mode="json"),
            },
        }
    )
    purged_candidate = AssemblyCandidateBundle.model_validate(executed_data)
    bundle_path = output_assembly_ref.with_name("purged_candidate.executed.json")
    bundle_source = stage_work_dir(
        state,
        "assembly_dedup",
        "purged_candidate.executed.json",
    )
    bundle_source.write_text(
        json.dumps(purged_candidate.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    bundle_artifact = ArtifactMeta.from_file(
        bundle_source,
        stage="assembly_dedup",
        method_name=ASSEMBLY_DEDUP_METHOD,
        artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        metadata={
            "candidate_id": purged_candidate.candidate_id,
            "dependency_refs": [
                published_plan.ref.model_dump(mode="json"),
                primary_output.model_dump(mode="json"),
                execution.evidence.stdout_ref.model_dump(mode="json"),
                execution.evidence.stderr_ref.model_dump(mode="json"),
            ],
        },
    )
    published_bundle = publish_artifact_record(
        data_dir,
        session_id,
        bundle_artifact,
        destination=bundle_path,
    )
    verified_purged = load_executed_candidate_bundle(
        data_dir,
        session_id,
        published_bundle.ref,
        expected_method_name=ASSEMBLY_DEDUP_METHOD,
        expected_candidate_id=candidate_id,
        expected_parameter_attempt_id=0,
    )
    if verified_purged.primary_output_ref != primary_output:
        raise ValueError("purged candidate primary output ref drifted")
    artifacts.extend([plan_artifact, bundle_artifact])
    register_artifact_records(data_dir, session_id, artifacts)
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger = Ledger(data_dir)
    attempt_id = attempt.attempt_id
    ledger.record_attempt_succeeded(
        session_id, attempt, artifacts=artifact_payloads
    )
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt_id)

    stage_update = _update_output(
        state,
        {
            "status": "completed",
            "purged_assembly_ref": str(verified_purged.primary_output_path),
            "purged_candidate_bundle_ref": verified_purged.ref.model_dump(
                mode="json"
            ),
            "purged_primary_output_ref": (
                verified_purged.primary_output_ref.model_dump(mode="json")
            ),
            "artifact_refs": [str(artifact.file_path) for artifact in artifacts],
        },
    )
    update: StateUpdate = {
        "current_stage": next_stage_name("assembly_dedup"),
        "supervision_state": "running_stage",
        "route_trace": append_route_trace(state, "assembly_dedup_stage"),
        **stage_update,
    }
    update["resource_refs"] = update_resource_ref(
        state,
        "assembly",
        [str(verified_purged.primary_output_path)],
        "assembly_dedup",
        {
            "candidate_bundle_ref": verified_purged.ref.model_dump(mode="json"),
            "primary_output_ref": verified_purged.primary_output_ref.model_dump(
                mode="json"
            ),
            "raw_candidate_bundle_ref": source_candidate.ref.model_dump(
                mode="json"
            ),
            "raw_primary_output_ref": (
                source_candidate.primary_output_ref.model_dump(mode="json")
            ),
            "deduplicated": True,
        },
    )
    return update


def fail_assembly_dedup(state: TopLevelState) -> StateUpdate:
    dedup_out = _output(state)
    return {
        "current_stage": "assembly_dedup",
        "supervision_state": "failed_terminal",
        "last_error": dedup_out.get("reason", "assembly_dedup failed"),
    }


def _selected_assembly_candidate(
    state: TopLevelState,
) -> VerifiedExecutedCandidate | None:
    resolution = resolve_stage_resource(
        state,
        resource_spec=ASSEMBLY_CANDIDATES_SPEC,
    )
    if not resolution.found:
        return None
    raw_ref = resolution.metadata.get("candidate_bundle_ref")
    if raw_ref is None:
        raise ValueError("assembly resource has no candidate_bundle_ref")
    verified = load_executed_candidate_bundle(
        absolute_data_dir(state),
        state["session_id"],
        raw_ref,
    )
    projected_primary = resolution.metadata.get("primary_output_ref")
    if projected_primary is None:
        raise ValueError("assembly resource has no primary_output_ref")
    if ArtifactRef.model_validate(projected_primary) != verified.primary_output_ref:
        raise ValueError(
            "assembly resource primary_output_ref does not match candidate bundle"
        )
    if list(resolution.paths) != [str(verified.primary_output_path)]:
        raise ValueError(
            "assembly resource paths do not match candidate primary output"
        )
    return verified


def _dedup_source_candidate(
    state: TopLevelState,
    output: AssemblyDedupCursor,
) -> VerifiedExecutedCandidate:
    raw_ref = output.get("candidate_bundle_ref")
    if raw_ref is None:
        raise ValueError("dedup cursor has no candidate_bundle_ref")
    verified = load_executed_candidate_bundle(
        absolute_data_dir(state),
        state["session_id"],
        raw_ref,
        expected_candidate_id=output.get("candidate_id"),
    )
    if (
        ArtifactRef.model_validate(output.get("input_primary_output_ref"))
        != verified.primary_output_ref
    ):
        raise ValueError(
            "dedup input_primary_output_ref does not match candidate bundle"
        )
    if Path(str(output.get("input_assembly_ref"))).resolve(strict=False) != (
        verified.primary_output_path.resolve(strict=False)
    ):
        raise ValueError(
            "dedup input assembly path does not match candidate primary output"
        )
    if (
        list(output.get("input_member_ids") or [])
        != verified.bundle.input_member_ids["primary"]
        or list(output.get("input_member_platforms") or [])
        != verified.bundle.input_member_platforms["primary"]
    ):
        raise ValueError("dedup primary member intent drifted from candidate bundle")
    return verified


def _selected_primary_members(
    state: TopLevelState,
    member_ids: list[str],
    member_platforms: list[str],
) -> list[RuntimeReadMember]:
    if not member_ids or len(member_ids) != len(member_platforms):
        raise ValueError("dedup primary member IDs/platforms are empty or mismatched")
    resolved = resolve_long_read_members(state)
    by_id: dict[str, RuntimeReadMember] = {}
    for member in resolved:
        if member.member_id in by_id:
            raise ValueError(
                f"runtime long-read member ID is duplicated: {member.member_id!r}"
            )
        by_id[member.member_id] = member
    selected: list[RuntimeReadMember] = []
    for member_id, platform in zip(member_ids, member_platforms, strict=True):
        member = by_id.get(member_id)
        if member is None:
            raise ValueError(
                f"candidate primary read member is unavailable: {member_id!r}"
            )
        if member.platform != platform:
            raise ValueError(
                f"candidate primary read platform drifted for {member_id!r}: "
                f"{member.platform!r} != {platform!r}"
            )
        selected.append(member)
    return selected


def _minimap2_preset(platform: str) -> str:
    normalized = platform.lower()
    if normalized == "pacbio_hifi":
        return "map-hifi"
    if normalized in {"clr", "pacbio_clr"}:
        return "map-pb"
    if normalized in {"ont", "cycloneseq"}:
        return "map-ont"
    raise ValueError(
        f"unsupported long-read platform for purge_dups mapping: {platform!r}"
    )
