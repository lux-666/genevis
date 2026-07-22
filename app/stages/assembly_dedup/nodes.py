"""Assembly deduplication stage nodes."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from app.configuration import load_descriptor_registry
from app.execution.env_dispatch import resolve_method_env
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
    AssemblyCandidateBundle,
)
from app.schemas.state import TopLevelState
from app.stages.base import (
    absolute_data_dir,
    append_route_trace,
    stage_artifacts_dir,
    stage_output,
    stage_work_dir,
    update_stage_output,
)
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import ASSEMBLY_CANDIDATES_SPEC, LONG_READS_SPEC
from app.storage.artifacts import publish_artifact_record, register_artifact_records
from app.storage.ledger import Ledger
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


def build_purge_dups_graph():
    """Build the shared purge_dups execution lifecycle."""

    return build_method_graph(
        PURGE_DUPS_LIFECYCLE,
        freeze_purge_dups_attempt,
        collect_purge_dups_attempt,
    )


def _output(state: TopLevelState) -> dict[str, Any]:
    return stage_output(state, "assembly_dedup")


def _update_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    return update_stage_output(state, "assembly_dedup", payload)


def _fail(state: TopLevelState, reason: str) -> dict:
    return _update_output(state, {"status": "failed", "reason": reason})


def enter_assembly_dedup(state: TopLevelState) -> dict:
    if _output(state).get("status"):
        return {}
    return {"supervision_state": "running_stage"}


def plan_purge_dups(state: TopLevelState) -> dict:
    assembly_out = dict((state.get("stage_outputs") or {}).get("assembly") or {})
    candidate = _selected_assembly_candidate(state)
    if candidate is None:
        return _update_output(
            state,
            {
                "status": "skipped",
                "skip_reason": "no_materialized_draft_assembly",
                "selected_candidate_refs": list(assembly_out.get("selected_candidate_refs") or []),
            },
        )
    candidate_ref, candidate_id, final_assembly_ref = candidate
    long_read_refs = _long_read_refs(state)
    if not long_read_refs:
        return _update_output(
            state,
            {
                "status": "skipped",
                "skip_reason": "no_long_reads_for_dedup_evidence",
                "candidate_ref": candidate_ref,
                "candidate_id": candidate_id,
                "input_assembly_ref": final_assembly_ref,
            },
        )

    dedup_dir = stage_artifacts_dir(state, "assembly_dedup")
    dedup_dir.mkdir(parents=True, exist_ok=True)
    return _update_output(
        state,
        {
            "status": "ready_to_run",
            "candidate_ref": candidate_ref,
            "candidate_id": candidate_id,
            "method_name": ASSEMBLY_DEDUP_METHOD,
            "input_assembly_ref": str(final_assembly_ref),
            "output_assembly_ref": str(dedup_dir / "purged_assembly.fasta"),
            "long_reads_ref": long_read_refs[0],
            "long_read_refs": long_read_refs,
            "read_mapping_preset": _read_mapping_preset(assembly_out),
            "cutoffs_strategy": "calcuts_auto",
            "get_seqs_exclude_haplotigs": True,
            "selected_candidate_refs": list(assembly_out.get("selected_candidate_refs") or []),
        },
    )


def freeze_purge_dups_attempt(state: TopLevelState) -> dict:
    dedup_out = _output(state)
    if dedup_out.get("status") != "ready_to_run":
        return {}

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    registry = load_descriptor_registry()
    if ASSEMBLY_DEDUP_METHOD not in registry.methods:
        return _fail(state, f"assembly dedup descriptor missing: {ASSEMBLY_DEDUP_METHOD}")

    env_spec, _packages, blocked = resolve_method_env(
        dedup_out,
        stage="assembly_dedup",
        method_name=ASSEMBLY_DEDUP_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_output(state, blocked)

    candidate_ref = dedup_out["candidate_ref"]
    input_assembly_ref = Path(dedup_out["input_assembly_ref"])
    output_assembly_ref = Path(dedup_out["output_assembly_ref"])
    long_read_refs = [str(path) for path in dedup_out.get("long_read_refs") or []]
    if not long_read_refs and dedup_out.get("long_reads_ref"):
        long_read_refs = [str(dedup_out["long_reads_ref"])]
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
    get_seqs_prefix = artifact_dir / "purged_assembly"
    get_seqs_primary = Path(f"{get_seqs_prefix}.purged.fa")
    script_path.write_text(
        "set -euo pipefail\n"
        "reads=(" + " ".join(shlex.quote(path) for path in long_read_refs) + ")\n"
        "if [[ ${#reads[@]} -eq 0 ]]; then\n"
        "  echo 'no long-read files found for purge_dups' >&2\n"
        "  exit 2\n"
        "fi\n"
        f"minimap2 -t 8 -ax {shlex.quote(str(dedup_out['read_mapping_preset']))} {shlex.quote(str(input_assembly_ref))} \"${{reads[@]}}\" "
        f"| samtools sort -@ 8 -o {shlex.quote(str(read_bam))}\n"
        f"samtools index {shlex.quote(str(read_bam))}\n"
        f"minimap2 -t 8 -x {shlex.quote(str(dedup_out['read_mapping_preset']))} {shlex.quote(str(input_assembly_ref))} \"${{reads[@]}}\" "
        f"| gzip -c > {shlex.quote(str(read_paf))}\n"
        f"cd {shlex.quote(str(artifact_dir))}\n"
        f"pbcstat {shlex.quote(str(read_paf))}\n"
        "test -s PB.base.cov && test -s PB.stat\n"
        f"calcuts {shlex.quote(str(pb_stat))} > {shlex.quote(str(cutoffs))}\n"
        f"split_fa {shlex.quote(str(input_assembly_ref))} > {shlex.quote(str(split_assembly))}\n"
        f"test -s {shlex.quote(str(split_assembly))}\n"
        f"minimap2 -t 8 -x asm5 -DP {shlex.quote(str(split_assembly))} {shlex.quote(str(split_assembly))} "
        f"| gzip -c > {shlex.quote(str(self_paf))}\n"
        f"purge_dups -2 -T {shlex.quote(str(cutoffs))} -c {shlex.quote(str(base_cov))} "
        f"{shlex.quote(str(self_paf))} > {shlex.quote(str(dups_bed))}\n"
        f"get_seqs -e -p {shlex.quote(str(get_seqs_prefix))} {shlex.quote(str(dups_bed))} {shlex.quote(str(input_assembly_ref))}\n"
        f"mv {shlex.quote(str(get_seqs_primary))} {shlex.quote(str(output_assembly_ref))}\n"
        f"test -s {shlex.quote(str(output_assembly_ref))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:assembly_dedup:purge_dups",
        session_id=session_id,
        stage="assembly_dedup",
        method_name=ASSEMBLY_DEDUP_METHOD,
        script_path=script_path,
        working_dir=work_dir,
        expected_outputs=[read_bam, read_paf, split_assembly, base_cov, pb_stat, cutoffs, self_paf, dups_bed, output_assembly_ref],
        env=dedup_out.get("env"),
        env_spec=env_spec,
        metadata={
            "candidate_ref": candidate_ref,
            "candidate_id": dedup_out.get("candidate_id"),
            "input_assembly_ref": str(input_assembly_ref),
            "output_assembly_ref": str(output_assembly_ref),
            "long_reads_refs": list(long_read_refs),
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


def collect_purge_dups_attempt(state: TopLevelState) -> dict:
    """Validate the completed purge_dups attempt before stage commit."""

    executions = load_method_executions(state, PURGE_DUPS_LIFECYCLE)
    if len(executions) != 1:
        return _fail(state, "purge_dups requires exactly one execution attempt")
    return _update_output(state, {"status": "dedup_ran"})


def commit_assembly_dedup(state: TopLevelState) -> dict:
    dedup_out = _output(state)
    if dedup_out.get("status") not in {"dedup_ran", "skipped"}:
        return {}

    attempt = None
    if dedup_out.get("status") == "dedup_ran":
        executions = load_method_executions(state, PURGE_DUPS_LIFECYCLE)
        if len(executions) != 1:
            return _fail(state, "purge_dups requires exactly one execution attempt")
        attempt = executions[0].attempt

    input_assembly_ref = dedup_out.get("input_assembly_ref")
    output_assembly_ref = dedup_out.get("output_assembly_ref") or input_assembly_ref
    source_candidate_ref = dedup_out.get("candidate_ref")
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    artifacts: list[ArtifactMeta] = []
    if output_assembly_ref and Path(str(output_assembly_ref)).exists():
        artifacts.append(
            ArtifactMeta.from_file(
                Path(str(output_assembly_ref)),
                stage="assembly_dedup",
                method_name=ASSEMBLY_DEDUP_METHOD,
                artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
                metadata={"candidate_ref": source_candidate_ref},
            )
        )
    attempt_metadata = dict(attempt.metadata) if attempt is not None else {}
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
                    metadata={"candidate_ref": source_candidate_ref},
                )
            )
    purged_candidate_ref = None
    if dedup_out.get("status") == "dedup_ran" and output_assembly_ref and Path(str(output_assembly_ref)).exists():
        bundle_path = Path(str(output_assembly_ref)).with_name("purged_candidate.json")
        purged_candidate = AssemblyCandidateBundle(
            session_id=session_id,
            candidate_id=f"{dedup_out.get('candidate_id') or session_id}:assembly_dedup.purge_dups",
            method_name=ASSEMBLY_DEDUP_METHOD,
            status="executed",
            input_ref=list(dedup_out.get("long_read_refs") or [dedup_out.get("long_reads_ref")]),
            input_type="clean_long_reads",
            expected_outputs={"draft_assembly_fasta": str(output_assembly_ref)},
            metadata={
                "raw_candidate_ref": source_candidate_ref,
                "raw_candidate_id": dedup_out.get("candidate_id"),
                "raw_assembly_ref": input_assembly_ref,
                "cutoffs_strategy": "calcuts_auto",
                "get_seqs_exclude_haplotigs": True,
            },
        )
        bundle_source = stage_work_dir(
            state, "assembly_dedup", "purged_candidate.json"
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
                "raw_candidate_ref": source_candidate_ref,
            },
        )
        purged_candidate_ref = str(
            publish_artifact_record(
                data_dir,
                session_id,
                bundle_artifact,
                destination=bundle_path,
            ).path
        )
        artifacts.append(bundle_artifact)
    register_artifact_records(data_dir, session_id, artifacts)
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger = Ledger(data_dir)
    attempt_id = None
    if attempt is not None:
        attempt_id = attempt.attempt_id
        ledger.record_attempt_succeeded(session_id, attempt, artifacts=artifact_payloads)
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt_id)

    outputs = dict(state.get("stage_outputs") or {})
    current_assembly_ref = (
        str(output_assembly_ref)
        if dedup_out.get("status") == "dedup_ran" and output_assembly_ref
        else input_assembly_ref
    )
    outputs["assembly_dedup"] = {
        **dedup_out,
        "status": "completed",
        "purged_assembly_ref": str(output_assembly_ref) if dedup_out.get("status") == "dedup_ran" else None,
        "purged_candidate_ref": purged_candidate_ref,
        "artifact_refs": [str(artifact.file_path) for artifact in artifacts],
    }
    update = {
        "current_stage": next_stage_name("assembly_dedup"),
        "supervision_state": "running_stage",
        "route_trace": append_route_trace(state, "assembly_dedup_stage"),
        "stage_outputs": outputs,
    }
    if current_assembly_ref:
        update["resource_refs"] = update_resource_ref(
            state,
            "assembly",
            [str(current_assembly_ref)],
            "assembly_dedup",
            {
                "candidate_bundle_refs": [purged_candidate_ref]
                if purged_candidate_ref
                else [],
                "raw_candidate_ref": source_candidate_ref,
                "deduplicated": dedup_out.get("status") == "dedup_ran",
            },
        )
    return update


def fail_assembly_dedup(state: TopLevelState) -> dict:
    dedup_out = _output(state)
    return {
        "current_stage": "assembly_dedup",
        "supervision_state": "failed_terminal",
        "last_error": dedup_out.get("reason", "assembly_dedup failed"),
    }


def _primary_candidate_ref(candidate_refs: list[str]) -> str:
    """Return the ordered primary assembly candidate for deduplication."""
    return candidate_refs[0]


def _selected_assembly_candidate(state: TopLevelState) -> tuple[str, str | None, str] | None:
    resolution = resolve_stage_resource(
        state,
        resource_spec=ASSEMBLY_CANDIDATES_SPEC,
    )
    if not resolution.found:
        return None
    selected_refs = list(resolution.metadata.get("candidate_bundle_refs") or [])

    # Dedup runs only on the ordered primary assembly candidate.  When multiple
    # candidate bundle refs exist, selected_candidate_refs[0] is the handoff
    # candidate chosen by the assembly stage.
    for candidate_ref in selected_refs:
        path = Path(str(candidate_ref))
        payload = _load_candidate_payload(path)
        if payload is None:
            continue
        final_ref = (payload.get("expected_outputs") or {}).get("draft_assembly_fasta")
        if not final_ref or not Path(str(final_ref)).exists():
            continue
        return str(path), payload.get("candidate_id"), str(final_ref)

    for draft_ref in resolution.paths:
        if not Path(str(draft_ref)).is_file():
            continue
        candidate_ref = _primary_candidate_ref(selected_refs or [str(draft_ref)])
        payload = _load_candidate_payload(Path(str(candidate_ref)))
        candidate_id = payload.get("candidate_id") if payload else None
        return str(candidate_ref), candidate_id, str(draft_ref)
    return None


def _load_candidate_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _long_read_refs(state: TopLevelState) -> list[str]:
    resolution = resolve_stage_resource(state, resource_spec=LONG_READS_SPEC)
    return list(resolution.paths) if resolution.found else []


def _read_mapping_preset(assembly_out: dict[str, Any]) -> str:
    platform = str(assembly_out.get("primary_long_read_platform") or assembly_out.get("platform") or "").lower()
    if platform in {"pacbio_hifi", "hifi"}:
        return "map-hifi"
    if platform in {"pacbio_clr", "clr", "pacbio"}:
        return "map-pb"
    return "map-ont"
