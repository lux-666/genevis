"""Evidence-driven telomere inspection and conservative T2T patching."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from langgraph.graph.state import CompiledStateGraph
from pydantic import TypeAdapter
from typing_extensions import TypedDict

from app.configuration import load_descriptor_registry
from app.control.workflow_plan import (
    advance_workflow_step,
    assembly_delivery,
    current_workflow_step_id,
    fail_workflow_step,
)
from app.execution.attempt_owners import ParameterAttemptOwner
from app.execution.env_dispatch import resolve_method_env
from app.execution.input_integrity import (
    artifact_input_dependency,
    asset_input_dependencies,
    verify_input_dependencies,
)
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_outcomes import MethodOutcome, settle_method_outcomes
from app.execution.method_runner import build_frozen_attempt
from app.execution.parameter_attempts import current_parameter_attempt
from app.review.base import ReviewPayload
from app.review.persistence import (
    persist_review_request,
)
from app.schemas.artifacts import (
    T2T_ALIGNMENT_OBSERVATIONS,
    T2T_ALIGNMENT_READS,
    T2T_ASSEMBLY_TELOMERE_SCAN,
    T2T_EXTENSION_CANDIDATES,
    T2T_FINISHING_REPORT,
    T2T_PATCH_RECORDS,
    T2T_PATCHED_ASSEMBLY,
    T2T_READ_ALIGNMENT_PAF,
    T2T_READBACK_VALIDATION,
    T2T_READS_TELOMERE_SCAN,
    T2T_STATUS_SUMMARY,
    T2T_TELOMERIC_READS,
    ArtifactRef,
    ArtifactRefJson,
    T2TFinishingBundle,
)
from app.schemas.execution import InputDependency
from app.schemas.resource_ref import ResourceRef
from app.schemas.state import (
    ActiveInterrupt,
    StateUpdate,
    TopLevelState,
)
from app.stages.base import (
    absolute_data_dir,
    append_route_trace,
    stage_work_dir,
)
from app.stages.read_resolution import resolve_long_read_members
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import assembly_resource_spec
from app.stages.sequences import open_sequence_text, read_sequences, reverse_complement
from app.stages.t2t.algorithms import terminal_signal
from app.stages.t2t.parameters.catalog import T2TMode, resolve_t2t_mode
from app.stages.t2t.parameters.execution import (
    T2TFrozenExecutionInputs,
    T2TLongReadInput,
    load_chromosome_names_snapshot,
    load_t2t_frozen_execution_inputs,
)
from app.stages.t2t.reconstruction import validate_reconstruction_outputs
from app.stages.t2t.script import reconstruction_script
from app.stages.t2t.state import (
    T2TConfiguredReadInputJson,
    T2TInterruptJson,
    T2TLongReadInputJson,
    T2TStageCursor,
    T2TStatus,
    read_t2t_stage_output,
    update_t2t_stage_output,
)
from app.storage.artifacts import (
    artifact_ref_for_path,
    publish_or_recover_work_artifact,
    resolve_artifact_ref,
)
from app.storage.assembly_candidates import (
    AssemblyCandidateBundleError,
    VerifiedExecutedCandidate,
    load_executed_candidate_bundle,
    publish_derived_assembly_candidate,
)
from app.storage.asset_registry import load_asset_registry
from app.storage.ledger import Ledger
from app.storage.paths import DataLayoutError

T2T_ALIGNMENT_METHOD = "t2t.alignment.minimap2"
T2T_CONTRACT_METHOD = "t2t.contract"


def _settle_terminal_alignment(
    state: TopLevelState,
    stage_update: StateUpdate | None = None,
) -> StateUpdate:
    """Settle minimap2 before a terminal path that bypasses stage routing."""

    projected = cast(TopLevelState, {**state, **(stage_update or {})})
    return settle_method_outcomes(
        projected,
        stage="t2t",
        outcomes=t2t_alignment_method_outcome(projected),
        expected_method_names=(T2T_ALIGNMENT_METHOD,),
    )


def t2t_alignment_method_outcome(state: TopLevelState) -> MethodOutcome | None:
    """Project the final minimap2 disposition from durable T2T evidence."""

    output = read_t2t_stage_output(state)
    if output.get("finishing"):
        return None
    status = str(output.get("status") or "")
    paf_ref = output.get("alignment_paf_artifact_ref")
    candidates_ref = output.get("telomere_extension_candidates_artifact_ref")
    if paf_ref is not None and candidates_ref is not None:
        return MethodOutcome(
            method_name=T2T_ALIGNMENT_METHOD,
            disposition="completed",
            refs=(
                ArtifactRef.model_validate(paf_ref),
                ArtifactRef.model_validate(candidates_ref),
            ),
        )

    skip_reason = output.get("candidate_skip_reason") or output.get("skip_reason")
    if skip_reason or output.get("execution_enabled") is False:
        return MethodOutcome(
            method_name=T2T_ALIGNMENT_METHOD,
            disposition="skipped",
            reason=str(skip_reason or "t2t local alignment execution is disabled"),
        )

    if status not in {"failed", "failed_terminal", "blocked_env", "missing_tool"}:
        return None

    attempt_statuses = {
        str(item.get("status") or "")
        for item in (output.get("parameter_attempts") or [])
        if isinstance(item, dict) and item.get("method_id") == T2T_ALIGNMENT_METHOD
    }
    started = bool(attempt_statuses.intersection({"executed", "execution_failed"}))
    return MethodOutcome(
        method_name=T2T_ALIGNMENT_METHOD,
        disposition="failed" if started else "not_started",
        reason=str(output.get("reason") or "t2t minimap2 did not complete"),
    )


T2T_ALIGNMENT_LIFECYCLE = MethodExecutionLifecycle(
    stage="t2t",
    name="t2t minimap2",
    method_name=T2T_ALIGNMENT_METHOD,
    frozen_status="alignment_frozen",
    ran_status="alignment_ran",
    owner=ParameterAttemptOwner(),
    outcome_policy=t2t_alignment_method_outcome,
)
_T2T_INTERRUPT_ADAPTER = TypeAdapter(T2TInterruptJson)


class _T2TConfig(TypedDict):
    mode: T2TMode
    chromosome_list_ref: str | None
    assembly_fasta_ref: str | None
    read_inputs: list[T2TConfiguredReadInputJson]


def _top_level_interrupt(value: T2TInterruptJson) -> ActiveInterrupt:
    """Convert the validated stage interrupt to the shared top-level cursor."""

    interrupt: ActiveInterrupt = {
        "stage": "t2t",
        "workflow_step_id": value["workflow_step_id"],
    }
    if "gate_id" in value:
        interrupt["gate_id"] = value["gate_id"]
    if "reason" in value:
        interrupt["reason"] = value["reason"]
    if "blocked_at" in value:
        interrupt["blocked_at"] = value["blocked_at"]
    if "review_request_id" in value:
        interrupt["review_request_id"] = value["review_request_id"]
    if "request_ref" in value:
        ref = value["request_ref"]
        interrupt["request_ref"] = {
            "schema_version": ref["schema_version"],
            "artifact_id": ref["artifact_id"],
            "path_ref": ref["path_ref"],
            "sha256": ref["sha256"],
            "size_bytes": ref["size_bytes"],
            "artifact_type": ref["artifact_type"],
        }
    if "request_hash" in value:
        interrupt["request_hash"] = value["request_hash"]
    if "attempt_id" in value:
        interrupt["attempt_id"] = value["attempt_id"]
    if "previous_attempt_id" in value:
        interrupt["previous_attempt_id"] = value["previous_attempt_id"]
    if "task" in value:
        interrupt["task"] = value["task"]
    if "recommendation_hash" in value:
        interrupt["recommendation_hash"] = value["recommendation_hash"]
    return interrupt


def build_t2t_alignment_graph() -> CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]:
    """Build the shared minimap2 alignment lifecycle."""

    graph = build_method_graph(
        T2T_ALIGNMENT_LIFECYCLE,
        freeze_t2t_alignment,
        build_t2t_candidates,
    )
    if not isinstance(graph, CompiledStateGraph):
        raise TypeError("T2T alignment lifecycle did not compile to a state graph")
    return graph


def _output(state: TopLevelState) -> T2TStageCursor:
    return read_t2t_stage_output(state)


def _update(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    return update_t2t_stage_output(state, payload)


def _fail(state: TopLevelState, reason: str) -> StateUpdate:
    return _update(state, {"status": "failed", "reason": reason})


def _fail_terminal(state: TopLevelState, reason: str) -> StateUpdate:
    """Project a failed cursor and settle the active frozen occurrence."""

    stage_update = _fail(state, reason)
    return {
        **_settle_terminal_alignment(state, stage_update),
        **stage_update,
        **fail_workflow_step(state, "t2t", reason),
    }


def enter_t2t(state: TopLevelState) -> StateUpdate:
    """Enter the single-pass executor under an explicit recommendation policy."""
    output = _output(state)
    mode = output.get("recommendation_mode")
    if mode not in {"advisor", "deterministic_baseline"}:
        return _fail(
            state, "T2T requires explicit advisor or deterministic_baseline mode"
        )
    if output.get("execution_mode") == "local" and mode != "deterministic_baseline":
        return _fail(state, "T2T single-pass execution requires deterministic_baseline")
    return {"supervision_state": "running_stage"}


def resolve_t2t_inputs(state: TopLevelState) -> StateUpdate:
    """Resolve the assembly and complete canonical long-read input list."""
    existing = _output(state)
    try:
        config = _config(existing)
        load_chromosome_names_snapshot(config.get("chromosome_list_ref"))
    except ValueError as exc:
        return _fail(state, f"invalid t2t configuration: {exc}")
    assembly = resolve_stage_resource(
        state,
        resource_spec=assembly_resource_spec(),
    )
    configured_assembly_ref = config.get("assembly_fasta_ref")
    assembly_ref = assembly.path
    if not assembly_ref:
        return _fail(state, "missing final assembly ref for t2t")
    if configured_assembly_ref and _canonical_path(configured_assembly_ref) != (
        _canonical_path(assembly_ref)
    ):
        return _fail(
            state,
            "t2t.assembly_fasta_ref must match the current adopted assembly",
        )
    source_stage = assembly.source
    assembly_path = Path(assembly_ref)
    try:
        assembly_dependency = _assembly_input_dependency(state, assembly_ref)
        verify_input_dependencies(state, [assembly_dependency])
    except (OSError, ValueError, DataLayoutError) as exc:
        return _fail(state, f"invalid t2t assembly provenance: {exc}")
    if (
        not assembly_path.exists()
        or not assembly_path.is_file()
        or not _looks_like_fasta(assembly_path)
    ):
        return _fail(state, f"t2t input assembly is not a valid FASTA: {assembly_ref}")

    try:
        read_inputs, reads_source = _long_read_inputs(state, config["read_inputs"])
        if read_inputs:
            read_dependencies = [
                _long_read_input_dependency(
                    state,
                    T2TLongReadInput.model_validate(item),
                )
                for item in read_inputs
            ]
            verify_input_dependencies(state, read_dependencies)
            _validate_long_read_inputs(read_inputs)
    except (OSError, ValueError, DataLayoutError) as exc:
        return _fail(state, f"invalid t2t long-read input: {exc}")
    work_dir = stage_work_dir(state, "t2t", "t2t_finishing")
    work_dir.mkdir(parents=True, exist_ok=True)
    return _update(
        state,
        {
            "status": "resolved",
            "mode": config["mode"],
            "chromosome_list_ref": config.get("chromosome_list_ref"),
            "input_assembly_ref": str(assembly_path),
            "source_stage": source_stage,
            "long_read_refs": [item["path"] for item in read_inputs],
            "long_read_inputs": read_inputs,
            "long_reads_source": reads_source,
            "execution_enabled": existing.get("execution_mode") == "local",
            "work_dir": str(work_dir),
            "output_assembly_ref": str(assembly_path),
            "notes": [],
        },
    )


def scan_assembly_telomeres(state: TopLevelState) -> StateUpdate:
    """Write per-contig terminal telomere evidence without changing the FASTA."""
    out = _output(state)
    if out.get("status") != "parameters_ready":
        return {}
    try:
        execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
        assembly_dependency = _assembly_input_dependency(
            state, execution_inputs.input_assembly_ref
        )
        verify_input_dependencies(state, [assembly_dependency])
        thresholds = execution_inputs.thresholds.model_dump(mode="json")
        rows = list(
            _scan_fasta_ends(
                Path(execution_inputs.input_assembly_ref),
                execution_inputs.resolved_telomere_motif,
                thresholds,
            )
        )
    except (OSError, ValueError, DataLayoutError) as exc:
        return _fail(state, f"t2t assembly telomere scan failed: {exc}")
    scan_source = Path(execution_inputs.work_dir) / "assembly_telomere_scan.tsv"
    _write_tsv(scan_source, rows, _ASSEMBLY_SCAN_FIELDS)
    scan_path = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        scan_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_ASSEMBLY_TELOMERE_SCAN,
    ).path
    chromosome_names = set(execution_inputs.chromosome_names)
    warnings = list(out.get("notes") or [])
    if chromosome_names:
        unknown = sorted(chromosome_names.difference({row["sequence"] for row in rows}))
        if unknown:
            warnings.append(
                f"chromosome_list_missing_from_assembly:{','.join(unknown)}"
            )
    if len(rows) > 100:
        warnings.append(
            "assembly_has_more_than_100_sequences; chromosome-level suitability "
            "is uncertain"
        )
    missing_ends = sum(
        int(row["left_has_signal"] == "false") + int(row["right_has_signal"] == "false")
        for row in rows
    )
    return _update(
        state,
        {
            "status": "assembly_scanned",
            "assembly_telomere_scan_ref": str(scan_path),
            "assembly_sequence_count": len(rows),
            "assembly_missing_end_count": missing_ends,
            "notes": warnings,
        },
    )


def prepare_t2t_reads(state: TopLevelState) -> StateUpdate:
    """Validate every resolved long-read path before scanning or mapping it."""
    out = _output(state)
    if out.get("status") not in {"resolved", "assembly_scanned"}:
        return {}
    if out.get("status") == "assembly_scanned":
        try:
            execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
        except ValueError as exc:
            return _fail(state, f"t2t frozen execution inputs are invalid: {exc}")
        read_inputs = []
        for item in execution_inputs.long_read_inputs:
            read_input: T2TLongReadInputJson = {
                "member_id": item.member_id,
                "source_asset_id": item.source_asset_id,
                "path": item.path,
                "platform": item.platform,
            }
            if item.read_artifact_ref is not None:
                read_input["read_artifact_ref"] = cast(
                    ArtifactRefJson,
                    item.read_artifact_ref.model_dump(mode="json"),
                )
            read_inputs.append(read_input)
    else:
        read_inputs = list(out.get("long_read_inputs") or [])
    if not read_inputs:
        return _skip_no_reads(state, out, "no_long_reads")
    try:
        validated = _validate_long_read_inputs(read_inputs)
    except (OSError, ValueError) as exc:
        return _fail(state, f"invalid t2t long-read input: {exc}")
    return _update(
        state,
        {
            "status": "reads_prepared",
            "long_read_inputs": validated,
        },
    )


def scan_read_telomeres(state: TopLevelState) -> StateUpdate:
    """Stream all long reads by declared platform; retain motif evidence separately."""
    out = _output(state)
    if out.get("status") != "reads_prepared":
        return {}
    read_count = 0
    telomeric_read_count = 0
    try:
        execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
        read_dependencies = [
            _long_read_input_dependency(state, item)
            for item in execution_inputs.long_read_inputs
        ]
        verify_input_dependencies(state, read_dependencies)
        thresholds = execution_inputs.thresholds.model_dump(mode="json")
        reads_scan_source = Path(execution_inputs.work_dir) / "reads_telomere_scan.tsv"
        telomeric_tsv_source = Path(execution_inputs.work_dir) / "telomeric_reads.tsv"
        alignment_fasta_sources: dict[str, Path] = {}
        with ExitStack() as stack:
            reads_handle = stack.enter_context(
                reads_scan_source.open("w", encoding="utf-8", newline="")
            )
            telomeric_handle = stack.enter_context(
                telomeric_tsv_source.open("w", encoding="utf-8", newline="")
            )
            reads_writer = csv.DictWriter(
                reads_handle,
                fieldnames=_READ_SCAN_FIELDS,
                delimiter="\t",
                extrasaction="ignore",
            )
            telomeric_writer = csv.DictWriter(
                telomeric_handle,
                fieldnames=_READ_SCAN_FIELDS,
                delimiter="\t",
                extrasaction="ignore",
            )
            reads_writer.writeheader()
            telomeric_writer.writeheader()
            seen_read_ids: set[str] = set()
            fasta_handles: dict[str, TextIO] = {}
            for read_input in execution_inputs.long_read_inputs:
                member_id = read_input.member_id
                preset = _minimap2_preset(read_input.platform)
                for source_read_id, sequence in read_sequences(Path(read_input.path)):
                    read_id = f"{member_id}::{source_read_id}"
                    if read_id in seen_read_ids:
                        raise ValueError(
                            f"duplicate read identifier within source asset: {read_id}"
                        )
                    seen_read_ids.add(read_id)
                    left, right = terminal_signal(
                        sequence,
                        execution_inputs.resolved_telomere_motif,
                        thresholds,
                    )
                    row = {
                        "read_id": read_id,
                        "source_asset_id": read_input.source_asset_id,
                        "source_member_id": member_id,
                        "source_read_id": source_read_id,
                        "platform": read_input.platform,
                        "minimap2_preset": preset,
                        "length": str(len(sequence)),
                        "left_motif_copies": str(left),
                        "right_motif_copies": str(right),
                        "left_has_signal": str(
                            left >= thresholds["min_motif_copies"]
                        ).lower(),
                        "right_has_signal": str(
                            right >= thresholds["min_motif_copies"]
                        ).lower(),
                    }
                    reads_writer.writerow(row)
                    read_count += 1
                    if (
                        row["left_has_signal"] == "true"
                        or row["right_has_signal"] == "true"
                    ):
                        telomeric_writer.writerow(row)
                        telomeric_read_count += 1
                    fasta_handle = fasta_handles.get(preset)
                    if fasta_handle is None:
                        fasta_source = (
                            Path(execution_inputs.work_dir)
                            / f"alignment_reads.{preset}.fa"
                        )
                        alignment_fasta_sources[preset] = fasta_source
                        fasta_handle = stack.enter_context(
                            fasta_source.open("w", encoding="utf-8")
                        )
                        fasta_handles[preset] = fasta_handle
                    _write_fasta_record(fasta_handle, read_id, sequence)
    except (OSError, ValueError, DataLayoutError) as exc:
        return _fail(state, f"t2t read telomere scan failed: {exc}")
    reads_scan = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        reads_scan_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_READS_TELOMERE_SCAN,
    ).path
    telomeric_tsv = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        telomeric_tsv_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_TELOMERIC_READS,
    ).path
    alignment_fasta_refs = {
        preset: str(
            publish_or_recover_work_artifact(
                absolute_data_dir(state),
                state["session_id"],
                source,
                stage="t2t",
                method_name=T2T_CONTRACT_METHOD,
                artifact_type=T2T_ALIGNMENT_READS,
                metadata={"minimap2_preset": preset},
            ).path
        )
        for preset, source in sorted(alignment_fasta_sources.items())
    }
    return _update(
        state,
        {
            "status": "reads_scanned",
            "reads_telomere_scan_ref": str(reads_scan),
            "telomeric_reads_ref": str(telomeric_tsv),
            "alignment_reads_fasta_refs": alignment_fasta_refs,
            "read_count": read_count,
            "telomeric_read_count": telomeric_read_count,
        },
    )


def freeze_t2t_alignment(state: TopLevelState) -> StateUpdate:
    """Freeze a minimap2 PAF alignment attempt with normal env preflight."""
    out = _output(state)
    if out.get("status") != "reads_scanned":
        return {}
    try:
        execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
    except ValueError as exc:
        return _fail(state, f"t2t frozen execution inputs are invalid: {exc}")
    if not out.get("read_count"):
        candidates_source = (
            Path(execution_inputs.work_dir) / "telomere_extension_candidates.tsv"
        )
        _write_tsv(candidates_source, [], _CANDIDATE_FIELDS)
        candidates_path = publish_or_recover_work_artifact(
            absolute_data_dir(state),
            state["session_id"],
            candidates_source,
            stage="t2t",
            method_name=T2T_CONTRACT_METHOD,
            artifact_type=T2T_EXTENSION_CANDIDATES,
        ).path
        return _update(
            state,
            {
                "status": "alignment_skipped",
                "candidate_skip_reason": "no_long_reads",
                "telomere_extension_candidates_ref": str(candidates_path),
                "candidate_count": 0,
            },
        )
    registry = load_descriptor_registry()
    if T2T_ALIGNMENT_METHOD not in registry.methods:
        return _fail(state, f"t2t descriptor missing: {T2T_ALIGNMENT_METHOD}")
    env_dispatch_input = {"env": dict(out.get("env") or {})}
    env_spec, _packages, blocked = resolve_method_env(
        env_dispatch_input,
        stage="t2t",
        method_name=T2T_ALIGNMENT_METHOD,
        workflow_step_id=current_workflow_step_id(state, expected_stage="t2t"),
        session_id=state["session_id"],
        data_dir=absolute_data_dir(state),
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked:
        return _update(state, blocked)
    work_dir = Path(execution_inputs.work_dir)
    paf_path = work_dir / "long_reads_to_assembly.paf"
    script_path = work_dir / "run_minimap2_t2t.sh"
    alignment_reads_refs = dict(out.get("alignment_reads_fasta_refs") or {})
    if not alignment_reads_refs:
        return _fail(
            state, "t2t alignment input FASTA groups are missing after read scan"
        )
    unsupported_presets = sorted(
        set(alignment_reads_refs).difference({"map-ont", "map-hifi", "map-pb"})
    )
    if unsupported_presets:
        return _fail(
            state,
            "t2t alignment input FASTA groups contain unsupported presets: "
            f"{unsupported_presets}",
        )
    script_path.write_text(
        reconstruction_script(
            {
                "work_dir": str(work_dir),
                "assembly": execution_inputs.input_assembly_ref,
                "assembly_scan": out["assembly_telomere_scan_ref"],
                "reads_scan": out["reads_telomere_scan_ref"],
                "reads": alignment_reads_refs,
                "thresholds": execution_inputs.thresholds.model_dump(mode="json"),
                "motif": execution_inputs.resolved_telomere_motif,
                "chromosome_names": list(execution_inputs.chromosome_names or []),
                "mode": execution_inputs.mode,
            }
        ),
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    parameter_attempt = None
    if not out.get("finishing"):
        parameter_attempt = current_parameter_attempt(
            out, method_id=T2T_ALIGNMENT_METHOD, label="T2T minimap2"
        )
        if parameter_attempt.frozen_parameters_ref is None:
            return _fail(state, "t2t minimap2 has no frozen parameter evidence")
    try:
        input_dependencies = [
            _assembly_input_dependency(state, execution_inputs.input_assembly_ref)
        ]
        for _preset, reads_ref in sorted(alignment_reads_refs.items()):
            reads_artifact_ref = artifact_ref_for_path(
                absolute_data_dir(state),
                state["session_id"],
                reads_ref,
                expected_type=T2T_ALIGNMENT_READS,
            )
            input_dependencies.append(
                artifact_input_dependency(state, reads_artifact_ref, reads_ref)
            )
        for scan_key in ("assembly_telomere_scan_ref", "reads_telomere_scan_ref"):
            input_dependencies.append(_assembly_input_dependency(state, out[scan_key]))
        if out.get("finishing"):
            frozen_parameters_ref = ArtifactRef.model_validate(
                out["finishing"]["frozen_ref"]
            )
            input_dependencies.append(
                artifact_input_dependency(
                    state,
                    frozen_parameters_ref,
                    resolve_artifact_ref(
                        absolute_data_dir(state),
                        state["session_id"],
                        frozen_parameters_ref,
                    ),
                )
            )
        verify_input_dependencies(state, input_dependencies)
    except (OSError, ValueError, DataLayoutError) as exc:
        return _fail(state, f"t2t alignment input dependency is invalid: {exc}")
    frozen_input_dependencies: list[InputDependency | dict[str, Any]] = [
        *input_dependencies
    ]
    attempt = build_frozen_attempt(
        attempt_id=(
            f"{state['session_id']}-{current_workflow_step_id(state)}-t2t-"
            f"{out['finishing']['attempt'] if out.get('finishing') else 0}"
        ),
        session_id=state["session_id"],
        stage="t2t",
        method_name=T2T_ALIGNMENT_METHOD,
        script_path=script_path,
        working_dir=work_dir,
        expected_outputs=[
            paf_path,
            *[
                work_dir / name
                for name in (
                    "reconstruction.json",
                    "reconstructed_assembly.fa",
                    "readback.paf",
                    "readback_validation.tsv",
                    "representative_baseline.fa",
                    "consensus_extensions.fa",
                )
            ],
        ],
        env=out.get("env"),
        env_spec=env_spec,
        input_dependencies=frozen_input_dependencies,
        metadata={
            "parameter_attempt_id": parameter_attempt.attempt_id
            if parameter_attempt
            else out["finishing"]["attempt"],
            "paf_path": str(paf_path),
            "assembly_ref": execution_inputs.input_assembly_ref,
            "reads_refs": alignment_reads_refs,
        },
    )
    from app.stages.finishing.workflow import t2t_lifecycle

    return freeze_method_attempts(
        state,
        t2t_lifecycle() if out.get("finishing") else T2T_ALIGNMENT_LIFECYCLE,
        [attempt],
    )


def build_t2t_candidates(state: TopLevelState) -> StateUpdate:
    """Combine terminal signals and PAF placements into patch candidates."""
    out = _output(state)
    if out.get("status") not in {"alignment_ran", "candidates_built"}:
        return {}
    if out.get("status") == "candidates_built":
        return {}
    from app.stages.finishing.workflow import t2t_lifecycle

    executions = load_method_executions(
        state, t2t_lifecycle() if out.get("finishing") else T2T_ALIGNMENT_LIFECYCLE
    )
    if len(executions) != 1:
        raise DataLayoutError("t2t minimap2 requires exactly one execution attempt")
    execution = executions[0]
    if not execution.evidence.success:
        raise DataLayoutError("t2t minimap2 execution did not succeed")
    paf_artifact = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        Path(str(execution.attempt.metadata.get("paf_path") or "")),
        stage="t2t",
        method_name=T2T_ALIGNMENT_METHOD,
        artifact_type=T2T_READ_ALIGNMENT_PAF,
        metadata={
            "attempt_id": execution.attempt.attempt_id,
            "execution_ref": execution.execution_ref.model_dump(mode="json"),
        },
    )
    if out.get("finishing"):
        frozen_attempt_ref = ArtifactRef.model_validate(
            out["execution_attempts"][0]["frozen_attempt_ref"]
        )
    else:
        parameter_attempt = current_parameter_attempt(
            out, method_id=T2T_ALIGNMENT_METHOD, label="T2T minimap2"
        )
        if parameter_attempt.frozen_attempt_ref is None:
            raise DataLayoutError("T2T parameter attempt has no frozen attempt ref")
        frozen_attempt_ref = parameter_attempt.frozen_attempt_ref
    frozen_ref_json = json.dumps(
        frozen_attempt_ref.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    execution_ref_json = json.dumps(
        execution.execution_ref.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
    additional = {
        name: publish_or_recover_work_artifact(
            absolute_data_dir(state),
            state["session_id"],
            Path(execution_inputs.work_dir) / name,
            stage="t2t",
            method_name=T2T_ALIGNMENT_METHOD,
            artifact_type=kind,
            metadata={
                "execution_ref": execution.execution_ref.model_dump(mode="json"),
                "frozen_attempt_ref": frozen_attempt_ref.model_dump(mode="json"),
            },
        )
        for name, kind in (
            ("readback.paf", T2T_READ_ALIGNMENT_PAF),
            ("readback_validation.tsv", T2T_READBACK_VALIDATION),
            ("reconstruction.json", "t2t.reconstruction"),
            ("reconstructed_assembly.fa", T2T_PATCHED_ASSEMBLY),
            ("representative_baseline.fa", "t2t.representative_baseline"),
            ("consensus_extensions.fa", "t2t.consensus_extensions"),
        )
    }
    try:
        execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
        work_dir = Path(execution_inputs.work_dir)
        candidates_source = work_dir / "telomere_extension_candidates.tsv"
        reconstruction = validate_reconstruction_outputs(
            additional["reconstruction.json"].path,
            additional["reconstructed_assembly.fa"].path,
            additional["readback_validation.tsv"].path,
        )
        raw = reconstruction["candidates"]
        observations = reconstruction["observations"]
        for row in [*raw, *observations]:
            row["frozen_attempt_ref"] = frozen_ref_json
            row["execution_ref"] = execution_ref_json
            row["alignment_paf_ref"] = json.dumps(
                paf_artifact.ref.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
    except OSError:
        raise
    except ValueError as exc:
        raise DataLayoutError(f"t2t candidate construction failed: {exc}") from exc
    _write_tsv(candidates_source, raw, _CANDIDATE_FIELDS)
    candidate_artifact = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        candidates_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_EXTENSION_CANDIDATES,
        metadata={
            "frozen_attempt_ref": frozen_attempt_ref.model_dump(mode="json"),
            "execution_ref": execution.execution_ref.model_dump(mode="json"),
            "alignment_paf_ref": paf_artifact.ref.model_dump(mode="json"),
        },
    )
    observations_source = Path(execution_inputs.work_dir) / "alignment_observations.tsv"
    _write_tsv(observations_source, observations, _ALIGNMENT_OBSERVATION_FIELDS)
    observations_artifact = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        observations_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_ALIGNMENT_OBSERVATIONS,
        metadata={
            "frozen_attempt_ref": frozen_attempt_ref.model_dump(mode="json"),
            "execution_ref": execution.execution_ref.model_dump(mode="json"),
            "alignment_paf_ref": paf_artifact.ref.model_dump(mode="json"),
        },
    )
    Ledger(absolute_data_dir(state)).record_attempt_succeeded(
        state["session_id"],
        execution.attempt,
        artifacts=[
            paf_artifact.ref.model_dump(mode="json"),
            candidate_artifact.ref.model_dump(mode="json"),
            observations_artifact.ref.model_dump(mode="json"),
            *[artifact.ref.model_dump(mode="json") for artifact in additional.values()],
        ],
    )
    selected = sum(row["decision"] == "auto_patch" for row in raw)
    proposed = sum(row["decision"] == "proposed" for row in raw)
    return _update(
        state,
        {
            "status": "candidates_built",
            "readback_validation_ref": str(additional["readback_validation.tsv"].path),
            "readback_paf_ref": str(additional["readback.paf"].path),
            "reconstruction_ref": str(additional["reconstruction.json"].path),
            "reconstructed_assembly_ref": str(
                additional["reconstructed_assembly.fa"].path
            ),
            "representative_baseline_ref": str(
                additional["representative_baseline.fa"].path
            ),
            "consensus_extensions_ref": str(additional["consensus_extensions.fa"].path),
            "alignment_paf_ref": str(paf_artifact.path),
            "alignment_paf_artifact_ref": paf_artifact.ref.model_dump(mode="json"),
            "telomere_extension_candidates_ref": str(candidate_artifact.path),
            "telomere_extension_candidates_artifact_ref": (
                candidate_artifact.ref.model_dump(mode="json")
            ),
            "candidate_count": len(raw),
            "alignment_observations_ref": str(observations_artifact.path),
            "selected_patch_count": selected,
            "proposed_candidate_count": proposed,
        },
    )


def apply_t2t_patches(state: TopLevelState) -> StateUpdate:
    """Materialize a new FASTA only for validated, auto-selected candidates."""
    out = _output(state)
    if out.get("status") != "candidates_built":
        return {}
    try:
        execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
    except ValueError as exc:
        return _fail(state, f"t2t frozen execution inputs are invalid: {exc}")
    if execution_inputs.mode != "auto_patch":
        return _update(
            state,
            {"status": "patch_not_applied", "patch_reason": "no_auto_patch_candidates"},
        )
    materialized = _materialize_t2t_patches(
        state,
        out,
        execution_inputs=execution_inputs,
        allowed_decisions={"auto_patch"},
    )
    if not materialized:
        return _update(
            state,
            {
                "status": "patch_not_applied",
                "patch_reason": "no_auto_patch_candidates",
            },
        )
    if materialized.get("error"):
        return _fail(
            state, f"t2t patch materialization failed: {materialized['error']}"
        )
    return _update(
        state,
        {
            "status": "patched",
            "patched_assembly_ref": materialized["patched_assembly_ref"],
            "patch_records_ref": materialized["patch_records_ref"],
        },
    )


def validate_patched_assembly(state: TopLevelState) -> StateUpdate:
    """Rescan patched ends and preserve the validation table as evidence."""
    out = _output(state)
    if out.get("status") != "patched":
        return {}
    try:
        execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
        thresholds = execution_inputs.thresholds.model_dump(mode="json")
        rows = list(
            _scan_fasta_ends(
                Path(out["patched_assembly_ref"] or ""),
                execution_inputs.resolved_telomere_motif,
                thresholds,
            )
        )
    except (OSError, ValueError) as exc:
        return _fail(state, f"t2t patched assembly validation failed: {exc}")
    if out.get("reconstructed_assembly_ref"):
        from app.integrity import sha256_file

        if sha256_file(Path(out["patched_assembly_ref"] or "")) != sha256_file(
            Path(out["reconstructed_assembly_ref"])
        ):
            return _fail(
                state, "T2T materialized assembly differs from read-back candidate"
            )
    source = Path(execution_inputs.work_dir) / "patched_telomere_scan.tsv"
    _write_tsv(source, rows, _ASSEMBLY_SCAN_FIELDS)
    path = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_ASSEMBLY_TELOMERE_SCAN,
    ).path
    return _update(
        state, {"status": "patched_validated", "patched_telomere_scan_ref": str(path)}
    )


def write_t2t_report(state: TopLevelState) -> StateUpdate:
    """Write final evidence or the immutable packet shown before human review."""
    out = _output(state)
    if out.get("status") == "failed":
        return {}
    report_out = out
    execution_inputs = None
    if out.get("parameter_attempts") or out.get("finishing"):
        try:
            execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
        except ValueError as exc:
            return _fail(state, f"t2t frozen execution inputs are invalid: {exc}")
        report_update = update_t2t_stage_output(
            state,
            {
                "resolved_telomere_motif": execution_inputs.resolved_telomere_motif,
                "resolved_telomere_motif_source": execution_inputs.motif_source,
                "input_assembly_ref": execution_inputs.input_assembly_ref,
                "long_read_refs": [
                    item.path for item in execution_inputs.long_read_inputs
                ],
                "mode": execution_inputs.mode,
                "chromosome_list_ref": execution_inputs.chromosome_list_ref,
                "work_dir": execution_inputs.work_dir,
                "execution_enabled": execution_inputs.execution_enabled,
            },
        )
        report_state: TopLevelState = {**state, **report_update}
        report_out = read_t2t_stage_output(report_state)
    summary_source = Path(report_out["work_dir"]) / "telomere_status_summary.tsv"
    scan_ref = out.get("patched_telomere_scan_ref") or out.get(
        "assembly_telomere_scan_ref"
    )
    rows = _read_tsv(Path(scan_ref)) if scan_ref and Path(scan_ref).exists() else []
    summary = [
        {
            "sequence": row["sequence"],
            "left_telomere": row["left_has_signal"],
            "right_telomere": row["right_has_signal"],
            "status": "both_terminal_signals"
            if row["left_has_signal"] == "true" and row["right_has_signal"] == "true"
            else "missing_signal",
        }
        for row in rows
    ]
    _write_tsv(
        summary_source,
        summary,
        ["sequence", "left_telomere", "right_telomere", "status"],
    )
    summary_path = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        summary_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_STATUS_SUMMARY,
    ).path
    final_status = _final_status(report_out, summary)
    evidence_phase: Literal["pre_review", "final"] = (
        "pre_review"
        if final_status == "blocked_human" or out.get("finishing")
        else "final"
    )
    final_assembly = out.get("output_assembly_ref") or report_out["input_assembly_ref"]
    report_path, bundle_path, artifacts = _publish_t2t_finishing_evidence(
        state,
        report_out,
        summary,
        summary_path=summary_path,
        status=final_status,
        output_assembly_ref=final_assembly,
        evidence_phase=evidence_phase,
    )
    return _update(
        state,
        {
            "status": final_status,
            "telomere_status_summary_ref": str(summary_path),
            "t2t_finishing_report_ref": str(report_path),
            "bundle_ref": str(bundle_path),
            "finishing_evidence_phase": evidence_phase,
            "artifact_refs": [str(path) for path in artifacts] + [str(bundle_path)],
            "output_assembly_ref": final_assembly,
        },
    )


def gate_t2t_review(state: TopLevelState) -> StateUpdate:
    """Block for candidate selection or post-patch human adoption."""
    out = _output(state)
    if out.get("status") != "blocked_human":
        return {}
    report_path = Path(out["t2t_finishing_report_ref"])
    summary_path = Path(out["telomere_status_summary_ref"])
    bundle_path = Path(out["bundle_ref"])
    return _block_for_t2t_review(
        state,
        out,
        report_path=report_path,
        summary_path=summary_path,
        bundle_path=bundle_path,
    )


def commit_t2t(state: TopLevelState) -> StateUpdate:
    """Advance with the patched or original assembly reference."""
    out = _output(state)
    input_assembly_ref = out.get("input_assembly_ref")
    if out.get("parameter_attempts") or out.get("finishing"):
        try:
            input_assembly_ref = load_t2t_frozen_execution_inputs(
                state, output=out
            ).input_assembly_ref
        except ValueError as exc:
            reason = f"t2t frozen execution inputs are invalid: {exc}"
            return _fail_terminal(state, reason)
    assembly_ref = out.get("output_assembly_ref") or input_assembly_ref
    if not assembly_ref:
        return _fail_terminal(state, "t2t has no input or output assembly")
    adopted_patch = bool(
        out.get("patched_assembly_ref")
        and str(assembly_ref) == str(out.get("patched_assembly_ref"))
    )
    try:
        current_assembly = resolve_stage_resource(
            state,
            resource_spec=assembly_resource_spec(),
        )
    except (OSError, TypeError, ValueError) as exc:
        return _fail_terminal(
            state, f"t2t assembly resource provenance is invalid: {exc}"
        )
    current_assembly_matches_output = bool(
        current_assembly.found
        and current_assembly.path
        and _canonical_path(current_assembly.path) == _canonical_path(str(assembly_ref))
    )
    current_assembly_matches_input = bool(
        current_assembly.found
        and current_assembly.path
        and input_assembly_ref
        and _canonical_path(current_assembly.path)
        == _canonical_path(str(input_assembly_ref))
    )
    if current_assembly_matches_output:
        try:
            current_candidate = _verify_t2t_adopted_candidate(
                state,
                current_assembly_metadata=current_assembly.metadata,
                assembly_path=str(assembly_ref),
            )
            raw_assembly_ref = (state.get("resource_refs") or {}).get("assembly")
            if raw_assembly_ref is None:
                raise ValueError("the central assembly ResourceRef is missing")
            persisted_assembly_ref = (
                ResourceRef.from_dict(raw_assembly_ref)
                if isinstance(raw_assembly_ref, dict)
                else raw_assembly_ref
            )
        except (AssemblyCandidateBundleError, OSError, TypeError, ValueError) as exc:
            return _fail_terminal(
                state, f"t2t output candidate provenance is invalid: {exc}"
            )
        update: StateUpdate = _advance_t2t_with_delivery(
            state,
            current_candidate,
            delivery_mode="materialized" if adopted_patch else "reused",
        )
        active_step_id = current_workflow_step_id(state, expected_stage="t2t")
        if (
            persisted_assembly_ref.source_stage == "t2t"
            and persisted_assembly_ref.workflow_step_id == active_step_id
        ):
            return update
        handoff_metadata = {
            **dict(persisted_assembly_ref.metadata),
            "patched": adopted_patch,
            "candidate_bundle_ref": current_candidate.ref.model_dump(mode="json"),
            "primary_output_ref": current_candidate.primary_output_ref.model_dump(
                mode="json"
            ),
        }
        update["resource_refs"] = update_resource_ref(
            state, "assembly", [str(assembly_ref)], "t2t", handoff_metadata
        )
        return update
    if not current_assembly_matches_input:
        return _fail_terminal(
            state,
            "t2t assembly resource changed since input resolution; "
            "refusing to overwrite a newer assembly",
        )
    try:
        source_candidate = _verify_t2t_adopted_candidate(
            state,
            current_assembly_metadata=current_assembly.metadata,
            assembly_path=str(input_assembly_ref),
        )
        assembly_dependency = _assembly_input_dependency(state, str(assembly_ref))
        verify_input_dependencies(state, [assembly_dependency])
    except (
        AssemblyCandidateBundleError,
        OSError,
        TypeError,
        ValueError,
        DataLayoutError,
    ) as exc:
        return _fail_terminal(
            state, f"t2t output assembly provenance is invalid: {exc}"
        )

    metadata: dict[str, Any] = {"patched": adopted_patch}
    if assembly_dependency.kind != "artifact":
        return _fail_terminal(state, "t2t output assembly must be a persisted artifact")
    if (
        adopted_patch
        and assembly_dependency.artifact_ref.artifact_type != T2T_PATCHED_ASSEMBLY
    ):
        return _fail_terminal(
            state,
            f"adopted t2t patch does not reference a {T2T_PATCHED_ASSEMBLY!r} artifact",
        )
    output_candidate = source_candidate
    if adopted_patch:
        try:
            decision_ref = _t2t_adoption_decision_ref(state, out)
            output_candidate = publish_derived_assembly_candidate(
                absolute_data_dir(state),
                state["session_id"],
                workflow_step_id=current_workflow_step_id(state, expected_stage="t2t"),
                source_candidate=source_candidate,
                primary_output_ref=assembly_dependency.artifact_ref,
                stage="t2t",
                method_name=T2T_CONTRACT_METHOD,
                action_identity=decision_ref.artifact_id,
            )
        except (AssemblyCandidateBundleError, OSError, TypeError, ValueError) as exc:
            return _fail_terminal(
                state, f"t2t adopted candidate publication failed: {exc}"
            )
        input_primary_ref = source_candidate.primary_output_ref.model_dump(mode="json")
        metadata.update(
            {
                "input_candidate_bundle_ref": source_candidate.ref.model_dump(
                    mode="json"
                ),
                "input_primary_output_ref": input_primary_ref,
            }
        )
    metadata.update(
        {
            "candidate_bundle_ref": output_candidate.ref.model_dump(mode="json"),
            "primary_output_ref": output_candidate.primary_output_ref.model_dump(
                mode="json"
            ),
        }
    )
    update = _advance_t2t_with_delivery(
        state,
        output_candidate,
        delivery_mode="materialized" if adopted_patch else "reused",
    )
    update["resource_refs"] = update_resource_ref(
        state, "assembly", [str(assembly_ref)], "t2t", metadata
    )
    return update


def _advance_t2t_with_delivery(
    state: TopLevelState,
    candidate: VerifiedExecutedCandidate,
    *,
    delivery_mode: Literal["materialized", "reused"],
) -> StateUpdate:
    return {
        **advance_workflow_step(
            state,
            "t2t",
            outcome="completed",
            deliveries=(
                assembly_delivery(
                    candidate.ref.model_dump(mode="json"),
                    candidate.primary_output_ref.model_dump(mode="json"),
                    delivery_mode=delivery_mode,
                ),
            ),
        ),
        "route_trace": append_route_trace(state, "t2t_stage"),
    }


def fail_t2t(state: TopLevelState) -> StateUpdate:
    """End the workflow only for invalid inputs or terminal tool failures."""
    out = _output(state)
    return _fail_terminal(state, str(out.get("reason", "t2t failed")))


def _verify_t2t_adopted_candidate(
    state: TopLevelState,
    *,
    current_assembly_metadata: Mapping[str, Any],
    assembly_path: str,
) -> VerifiedExecutedCandidate:
    """Load the exact current candidate that produced the T2T input assembly."""

    candidate_ref = current_assembly_metadata.get("candidate_bundle_ref")
    if candidate_ref is None:
        raise AssemblyCandidateBundleError(
            "t2t assembly handoff requires the current candidate_bundle_ref"
        )
    source_candidate = load_executed_candidate_bundle(
        absolute_data_dir(state), state["session_id"], candidate_ref
    )
    if _canonical_path(str(source_candidate.primary_output_path)) != _canonical_path(
        assembly_path
    ):
        raise AssemblyCandidateBundleError(
            "current candidate primary output does not match the t2t assembly"
        )
    return source_candidate


def _t2t_adoption_decision_ref(
    state: TopLevelState,
    out: T2TStageCursor,
) -> ArtifactRef:
    """Resolve the immutable decision used as derived-candidate identity."""

    decision_ref = ArtifactRef.model_validate(out.get("review_decision_artifact_ref"))
    if decision_ref.artifact_type != "t2t.review_decision":
        raise AssemblyCandidateBundleError(
            "adopted t2t patch requires its persisted review decision artifact"
        )
    resolve_artifact_ref(absolute_data_dir(state), state["session_id"], decision_ref)
    return decision_ref


def _config(out: T2TStageCursor) -> _T2TConfig:
    read_inputs = list(out.get("read_inputs") or [])
    return {
        "mode": resolve_t2t_mode(out.get("mode")),
        "chromosome_list_ref": out.get("chromosome_list_ref"),
        "assembly_fasta_ref": out.get("assembly_fasta_ref"),
        "read_inputs": read_inputs,
    }


def _minimap2_preset(platform: str) -> str:
    """Return the minimap2 -x preset matching the long-read platform."""
    presets = {
        "ont": "map-ont",
        "pacbio_hifi": "map-hifi",
        "clr": "map-pb",
        "cycloneseq": "map-ont",
    }
    try:
        return presets[platform]
    except KeyError as exc:
        raise ValueError(
            f"unsupported canonical long-read platform: {platform!r}"
        ) from exc


def _long_read_inputs(
    state: TopLevelState,
    configured_inputs: list[T2TConfiguredReadInputJson],
) -> tuple[list[T2TLongReadInputJson], str]:
    """Resolve authoritative members through the shared runtime resolver only."""

    override_paths = [item["path"] for item in configured_inputs]
    configured_by_source_asset = _configured_source_assets(state, configured_inputs)
    members = resolve_long_read_members(
        state,
        override_paths=override_paths or None,
    )
    if not members:
        return [], "missing"

    if configured_by_source_asset:
        seen_source_assets: set[str] = set()
        for member in members:
            configured = configured_by_source_asset.get(member.source_asset_id)
            if configured is None:
                raise ValueError(
                    "resolved long-read member has no configured source lineage: "
                    f"{member.path}"
                )
            if member.source_asset_id in seen_source_assets:
                raise ValueError(
                    "configured source asset resolved to multiple long-read members: "
                    f"{member.source_asset_id}"
                )
            seen_source_assets.add(member.source_asset_id)
            if configured["platform"] != member.platform:
                raise ValueError(
                    "configured long-read platform disagrees with registered "
                    f"provenance for {member.path}"
                )
        missing_sources = sorted(
            set(configured_by_source_asset).difference(seen_source_assets)
        )
        if missing_sources:
            raise ValueError(
                f"configured long-read sources were not resolved: {missing_sources}"
            )

    source = _long_reads_source(state, configured=bool(configured_inputs))
    inputs: list[T2TLongReadInputJson] = []
    for member in members:
        read_input: T2TLongReadInputJson = {
            "member_id": member.member_id,
            "source_asset_id": member.source_asset_id,
            "path": member.path,
            "platform": member.platform,
        }
        if member.read_artifact_ref is not None:
            read_input["read_artifact_ref"] = cast(
                ArtifactRefJson,
                member.read_artifact_ref.model_dump(mode="json"),
            )
        inputs.append(read_input)
    return inputs, source


def _configured_source_assets(
    state: TopLevelState,
    configured_inputs: list[T2TConfiguredReadInputJson],
) -> dict[str, T2TConfiguredReadInputJson]:
    if not configured_inputs:
        return {}
    registry = load_asset_registry(state)
    configured_by_asset: dict[str, T2TConfiguredReadInputJson] = {}
    for configured in configured_inputs:
        configured_path = _canonical_path(configured["path"])
        matches = [
            record
            for record in registry.assets
            if _canonical_path(record.path) == configured_path
        ]
        if len(matches) != 1:
            raise ValueError(
                "configured long-read path must uniquely match one registry asset: "
                f"{configured['path']}"
            )
        record = matches[0]
        if record.platform != configured["platform"]:
            raise ValueError(
                "configured long-read platform disagrees with its registry asset: "
                f"{configured['path']}"
            )
        if record.asset_id in configured_by_asset:
            raise ValueError(f"duplicate configured long-read asset: {record.asset_id}")
        configured_by_asset[record.asset_id] = configured
    return configured_by_asset


def _long_reads_source(
    state: TopLevelState,
    *,
    configured: bool,
) -> Literal[
    "run_config",
    "read_set_manifest",
    "asset_registry",
    "resource_ref",
]:
    if configured:
        return "run_config"
    raw_ref = (state.get("resource_refs") or {}).get("long_reads")
    if not isinstance(raw_ref, Mapping):
        return "asset_registry"
    metadata = raw_ref.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("read_set_manifest_ref"):
        return "read_set_manifest"
    if raw_ref.get("source_stage") in {None, "inventory"}:
        return "asset_registry"
    return "resource_ref"


def _validate_long_read_inputs(
    read_inputs: list[T2TLongReadInputJson],
) -> list[T2TLongReadInputJson]:
    validated: list[T2TLongReadInputJson] = []
    for item in read_inputs:
        member_id = str(item.get("member_id") or "").strip()
        source_asset_id = str(item.get("source_asset_id") or "").strip()
        reads_path = Path(str(item.get("path") or ""))
        platform = str(item.get("platform") or "").strip()
        if not member_id:
            raise ValueError("long-read input has no stable member_id")
        if not source_asset_id:
            raise ValueError("long-read input has no source_asset_id")
        if not platform:
            raise ValueError(f"long-read input {member_id!r} has no platform")
        _minimap2_preset(platform)
        if not reads_path.exists() or not reads_path.is_file():
            raise ValueError(f"long-read input is not a file: {reads_path}")
        try:
            next(read_sequences(reads_path))
        except StopIteration as exc:
            raise ValueError(f"long-read input is empty: {reads_path}") from exc
        except (OSError, ValueError) as exc:
            raise ValueError(f"malformed long-read input {reads_path}: {exc}") from exc
        validated_item: T2TLongReadInputJson = {
            "member_id": member_id,
            "source_asset_id": source_asset_id,
            "path": str(reads_path),
            "platform": platform,
        }
        if item.get("read_artifact_ref") is not None:
            validated_item["read_artifact_ref"] = item["read_artifact_ref"]
        validated.append(validated_item)
    return validated


def _long_read_input_dependency(
    state: TopLevelState,
    read_input: T2TLongReadInput,
) -> InputDependency:
    if read_input.read_artifact_ref is not None:
        return artifact_input_dependency(
            state,
            read_input.read_artifact_ref,
            read_input.path,
        )
    dependencies = asset_input_dependencies(
        state,
        [read_input.source_asset_id],
        [read_input.path],
    )
    return dependencies[0]


def _assembly_input_dependency(
    state: TopLevelState,
    assembly_path: str,
) -> InputDependency:
    try:
        artifact_ref = artifact_ref_for_path(
            absolute_data_dir(state),
            state["session_id"],
            assembly_path,
        )
    except (OSError, ValueError, DataLayoutError):
        artifact_ref = None
    if artifact_ref is not None:
        return artifact_input_dependency(state, artifact_ref, assembly_path)

    canonical_path = _canonical_path(assembly_path)
    assembly_asset_types = {
        "assembly_fasta_registered",
        "draft_assembly",
        "chromosome_scaffolded_assembly",
    }
    matches = [
        record
        for record in load_asset_registry(state).assets
        if record.asset_type in assembly_asset_types
        and _canonical_path(record.path) == canonical_path
    ]
    if len(matches) != 1:
        raise ValueError(
            "assembly input must be one run-owned artifact or uniquely registered "
            f"assembly asset: {assembly_path}"
        )
    dependencies = asset_input_dependencies(
        state,
        [matches[0].asset_id],
        [assembly_path],
    )
    return dependencies[0]


def _canonical_path(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve(strict=False))


def _skip_no_reads(
    state: TopLevelState,
    out: T2TStageCursor,
    reason: str,
) -> StateUpdate:
    work_dir = Path(out["work_dir"])
    reads_source = work_dir / "reads_telomere_scan.tsv"
    telomeric_source = work_dir / "telomeric_reads.tsv"
    candidates_source = work_dir / "telomere_extension_candidates.tsv"
    _write_tsv(reads_source, [], _READ_SCAN_FIELDS)
    _write_tsv(telomeric_source, [], _READ_SCAN_FIELDS)
    _write_tsv(candidates_source, [], _CANDIDATE_FIELDS)
    reads_scan = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        reads_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_READS_TELOMERE_SCAN,
    ).path
    telomeric_tsv = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        telomeric_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_TELOMERIC_READS,
    ).path
    candidates = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        candidates_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_EXTENSION_CANDIDATES,
    ).path
    return _update(
        state,
        {
            "status": "skipped_no_long_reads",
            "skip_reason": reason,
            "reads_telomere_scan_ref": str(reads_scan),
            "telomeric_reads_ref": str(telomeric_tsv),
            "telomere_extension_candidates_ref": str(candidates),
            "candidate_count": 0,
        },
    )


def _looks_like_fasta(path: Path) -> bool:
    try:
        with open_sequence_text(path) as handle:
            return handle.read(1) == ">"
    except (OSError, UnicodeError):
        return False


def _load_alignment_read_sequences(
    fasta_refs: Mapping[str, str],
    *,
    selected_ids: set[str] | None = None,
) -> dict[str, str]:
    sequences: dict[str, str] = {}
    seen: set[str] = set()
    for _preset, path_value in sorted(fasta_refs.items()):
        for read_id, sequence in read_sequences(Path(path_value)):
            if read_id in seen:
                raise ValueError(
                    f"duplicate alignment read across preset groups: {read_id}"
                )
            seen.add(read_id)
            if selected_ids is None or read_id in selected_ids:
                sequences[read_id] = sequence
    return sequences


def _scan_fasta_ends(
    path: Path, motif: str, thresholds: dict[str, int]
) -> Iterable[dict[str, str]]:
    seen: set[str] = set()
    for name, sequence in read_sequences(path):
        if name in seen:
            raise ValueError(f"duplicate scaffold identifier: {name}")
        seen.add(name)
        left, right = terminal_signal(sequence, motif, thresholds)
        yield {
            "sequence": name,
            "length": str(len(sequence)),
            "left_motif_copies": str(left),
            "right_motif_copies": str(right),
            "left_has_signal": str(left >= thresholds["min_motif_copies"]).lower(),
            "right_has_signal": str(right >= thresholds["min_motif_copies"]).lower(),
        }


def _write_patched_fasta(
    source: Path,
    destination: Path,
    patches: dict[str, dict[str, Any]],
) -> None:
    with destination.open("w", encoding="utf-8") as handle:
        for name, sequence in read_sequences(source):
            plan = patches.get(name) or {}
            left = plan.get("left")
            right = plan.get("right")
            left_cut = int(left["cut"]) if left else 0
            right_cut = int(right["cut"]) if right else len(sequence)
            if left_cut < 0 or right_cut > len(sequence) or left_cut > right_cut:
                raise ValueError(
                    f"invalid T2T patch interval for {name}: {left_cut}>{right_cut}"
                )
            patched = (
                (str(left["tail"]) if left else "")
                + sequence[left_cut:right_cut]
                + (str(right["tail"]) if right else "")
            )
            _write_fasta_record(handle, name, patched)


def _write_fasta_record(handle: TextIO, name: str, sequence: str) -> None:
    handle.write(f">{name}\n")
    for offset in range(0, len(sequence), 80):
        handle.write(sequence[offset : offset + 80] + "\n")


def _write_tsv(path: Path, rows: Iterable[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, delimiter="\t", extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _review_candidates(
    path: Path,
    *,
    allowed_decisions: set[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in _read_tsv(path):
        if row.get("decision") not in allowed_decisions:
            continue
        candidates.append(
            {
                "candidate_id": row["candidate_id"],
                "target": row["target"],
                "target_end": row["target_end"],
                "read_id": row["read_id"],
                "supporting_reads": row["supporting_reads"],
                "extension_length": row["extension_length"],
                "decision": row["decision"],
                "reason": row["reason"],
            }
        )
    return candidates


def _materialize_t2t_patches(
    state: TopLevelState,
    out: T2TStageCursor,
    *,
    execution_inputs: T2TFrozenExecutionInputs | None = None,
    selected_candidate_ids: set[str] | None = None,
    allowed_decisions: set[str],
) -> dict[str, str] | None:
    if execution_inputs is None:
        execution_inputs = load_t2t_frozen_execution_inputs(state, output=out)
    candidates = _read_tsv(Path(out["telomere_extension_candidates_ref"]))
    selected = [
        row
        for row in candidates
        if row.get("decision") in allowed_decisions
        and (
            selected_candidate_ids is None
            or row.get("candidate_id") in selected_candidate_ids
        )
    ]
    if not selected:
        return None
    alignment_sequences = _load_alignment_read_sequences(
        out.get("alignment_reads_fasta_refs") or {},
        selected_ids={row["read_id"] for row in selected},
    )
    assembly_lengths: dict[str, int] = {}
    for name, scaffold_sequence in read_sequences(
        Path(execution_inputs.input_assembly_ref)
    ):
        if name in assembly_lengths:
            raise ValueError(f"duplicate scaffold identifier: {name}")
        assembly_lengths[name] = len(scaffold_sequence)
    patches: dict[str, dict[str, Any]] = {}
    records: list[dict[str, str]] = []
    error: str | None = None
    for row in selected:
        sequence = alignment_sequences.get(row["read_id"])
        if not sequence:
            error = f"selected_read_sequence_unavailable:{row['read_id']}"
            break
        oriented = sequence if row["strand"] == "+" else reverse_complement(sequence)
        oriented_start = int(row["oriented_query_start"])
        oriented_end = int(row["oriented_query_end"])
        tail = (
            oriented[:oriented_start]
            if row["target_end"] == "left"
            else oriented[oriented_end:]
        )
        if row.get("reconstruction_method") == "poa_consensus":
            tail = row["consensus_sequence"]
        if not tail:
            error = f"empty_replacement_tail:{row['candidate_id']}"
            break
        target_length = assembly_lengths.get(row["target"])
        if target_length is None:
            error = f"patch_target_missing:{row['target']}"
            break
        cut = int(row["junction_coordinate"])
        if cut < 0 or cut > target_length:
            error = f"invalid_junction_coordinate:{row['candidate_id']}:{cut}"
            break
        target_plan = patches.setdefault(row["target"], {})
        if row["target_end"] in target_plan:
            continue
        target_plan[row["target_end"]] = {"tail": tail, "cut": cut, "row": row}
        records.append(
            {
                "candidate_id": row["candidate_id"],
                "target": row["target"],
                "target_end": row["target_end"],
                "source_read_id": row["read_id"],
                "source_asset_id": row.get("source_asset_id", ""),
                "original_read_id": row.get("source_read_id", row["read_id"]),
                "source_strand": row["strand"],
                "extension_length": str(len(tail)),
                "target_cut_coordinate": str(cut),
                "target_bases_removed": str(
                    cut if row["target_end"] == "left" else target_length - cut
                ),
                "bases_added": str(len(tail)),
                "supporting_reads": row["supporting_reads"],
                "mapq": row["mapq"],
                "overlap_bp": row["overlap_bp"],
                "reconstruction_method": row.get(
                    "reconstruction_method", "representative_tail"
                ),
                "construction_read_ids": row.get("construction_read_ids", ""),
                "evidence_scope": row.get("evidence_scope", "in_sample"),
                "materialization_status": "selected",
                "reason": "junction_coordinates_validated",
            }
        )
    if not error:
        for target, plan in patches.items():
            if "left" in plan and "right" in plan:
                left_cut = int(plan["left"]["cut"])
                right_cut = int(plan["right"]["cut"])
                if left_cut > right_cut:
                    error = f"conflicting_junctions:{target}:{left_cut}>{right_cut}"
                    break
    if not patches and not error:
        return None
    records_source = Path(execution_inputs.work_dir) / "patch_records.tsv"
    if error:
        for record in records:
            record["materialization_status"] = "not_materialized"
            record["reason"] = error
    _write_tsv(records_source, records, _PATCH_RECORD_FIELDS)
    records_path = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        records_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_PATCH_RECORDS,
    ).path
    if error:
        return {"patch_records_ref": str(records_path), "error": error}
    patched_source = Path(execution_inputs.work_dir) / "patched_assembly.fa"
    _write_patched_fasta(
        Path(execution_inputs.input_assembly_ref), patched_source, patches
    )
    patched_path = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        patched_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_PATCHED_ASSEMBLY,
    ).path
    return {
        "patched_assembly_ref": str(patched_path),
        "patch_records_ref": str(records_path),
    }


def _final_status(
    out: T2TStageCursor,
    summary: list[dict[str, str]],
) -> T2TStatus:
    if not out.get("execution_enabled"):
        return "skipped"
    if out.get("status") == "skipped_no_long_reads":
        return "skipped_no_long_reads"
    if (
        out.get("mode") == "auto_patch"
        and out.get("selected_patch_count", 0)
        and out.get("patched_assembly_ref")
        and out.get("patched_telomere_scan_ref")
    ):
        return "blocked_human"
    if out.get("mode") == "manual_review" and out.get("proposed_candidate_count", 0):
        return "blocked_human"
    if out.get("candidate_count", 0):
        return "completed_with_warnings"
    if any(row["status"] == "missing_signal" for row in summary) or out.get("notes"):
        return "completed_with_warnings"
    return "completed"


def _publish_t2t_finishing_evidence(
    state: TopLevelState,
    out: T2TStageCursor,
    summary: list[dict[str, str]],
    *,
    summary_path: Path,
    status: T2TStatus,
    output_assembly_ref: str,
    evidence_phase: Literal["pre_review", "final"],
) -> tuple[Path, Path, list[Path]]:
    """Publish one immutable pre-review or final report/bundle pair."""

    stem_suffix = ".pre_review" if evidence_phase == "pre_review" else ""
    report_source = Path(out["work_dir"]) / f"t2t_finishing_report{stem_suffix}.md"
    report_source.write_text(
        _report_markdown(
            out,
            summary,
            status=status,
            output_assembly_ref=output_assembly_ref,
            evidence_phase=evidence_phase,
        ),
        encoding="utf-8",
    )
    report_path = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        report_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_FINISHING_REPORT,
    ).path

    artifacts_by_path = {
        str(path): path
        for path in (
            Path(value)
            for value in out.get("artifact_refs") or []
            if Path(value).is_file()
        )
    }
    artifacts_by_path.update(
        {
            str(path): path
            for path in _register_artifacts(state, out, summary_path, report_path)
        }
    )
    artifacts = list(artifacts_by_path.values())
    bundle_source = Path(out["work_dir"]) / f"t2t_finishing_bundle{stem_suffix}.json"
    bundle = T2TFinishingBundle(
        session_id=state["session_id"],
        evidence_phase=evidence_phase,
        status=status,
        input_assembly_ref=out["input_assembly_ref"],
        output_assembly_ref=output_assembly_ref,
        long_read_refs=list(out.get("long_read_refs") or []),
        mode=out["mode"],
        resolved_telomere_motif=out.get("resolved_telomere_motif"),
        resolved_telomere_motif_source=out.get("resolved_telomere_motif_source"),
        patched_assembly_ref=out.get("patched_assembly_ref"),
        artifact_refs={path.name: str(path) for path in artifacts},
        notes=list(out.get("notes") or []),
        metadata={
            "source_stage": out.get("source_stage"),
            "execution_enabled": out.get("execution_enabled"),
            "review_decision": out.get("decision"),
        },
    )
    bundle_source.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    bundle_path = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        bundle_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type="t2t.bundle",
    ).path
    return report_path, bundle_path, artifacts


def _finalize_t2t_evidence(
    state: TopLevelState,
    out: T2TStageCursor,
) -> StateUpdate:
    """Replace cursor refs with post-review evidence bound to the adopted output."""

    output_assembly_ref = out["output_assembly_ref"]
    patched_assembly_ref = out.get("patched_assembly_ref")
    if patched_assembly_ref and output_assembly_ref == patched_assembly_ref:
        scan_ref = out.get("patched_telomere_scan_ref")
    else:
        scan_ref = out.get("assembly_telomere_scan_ref")
    if not scan_ref or not Path(scan_ref).is_file():
        raise FileNotFoundError(
            scan_ref or "no telomere scan exists for the adopted assembly"
        )
    summary = [
        {
            "sequence": row["sequence"],
            "left_telomere": row["left_has_signal"],
            "right_telomere": row["right_has_signal"],
            "status": "both_terminal_signals"
            if row["left_has_signal"] == "true" and row["right_has_signal"] == "true"
            else "missing_signal",
        }
        for row in _read_tsv(Path(scan_ref))
    ]
    status: T2TStatus = (
        "completed_with_warnings"
        if any(row["status"] == "missing_signal" for row in summary) or out.get("notes")
        else "completed"
    )
    summary_source = Path(out["work_dir"]) / "telomere_status_summary.final.tsv"
    _write_tsv(
        summary_source,
        summary,
        ["sequence", "left_telomere", "right_telomere", "status"],
    )
    summary_path = publish_or_recover_work_artifact(
        absolute_data_dir(state),
        state["session_id"],
        summary_source,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type=T2T_STATUS_SUMMARY,
    ).path
    report_path, bundle_path, artifacts = _publish_t2t_finishing_evidence(
        state,
        out,
        summary,
        summary_path=summary_path,
        status=status,
        output_assembly_ref=output_assembly_ref,
        evidence_phase="final",
    )
    return update_t2t_stage_output(
        state,
        {
            "status": status,
            "telomere_status_summary_ref": str(summary_path),
            "t2t_finishing_report_ref": str(report_path),
            "bundle_ref": str(bundle_path),
            "finishing_evidence_phase": "final",
            "artifact_refs": [str(path) for path in artifacts] + [str(bundle_path)],
        },
    )


def _block_for_t2t_review(
    state: TopLevelState,
    out: T2TStageCursor,
    *,
    report_path: Path,
    summary_path: Path,
    bundle_path: Path,
) -> StateUpdate:
    auto_adoption = out.get("mode") == "auto_patch"
    reason = (
        "t2t_candidate_patch_requires_human_adoption"
        if auto_adoption
        else "t2t_patch_candidates_require_human_selection"
    )
    candidates = _review_candidates(
        Path(out["telomere_extension_candidates_ref"]),
        allowed_decisions={"auto_patch"} if auto_adoption else {"proposed"},
    )
    report_refs = {
        "t2t_finishing_report_ref": str(report_path),
        "telomere_status_summary_ref": str(summary_path),
        "telomere_extension_candidates_ref": out["telomere_extension_candidates_ref"],
        "bundle_ref": str(bundle_path),
    }
    if out.get("patched_assembly_ref"):
        report_refs["patched_assembly_ref"] = str(out["patched_assembly_ref"])
    if out.get("patched_telomere_scan_ref"):
        report_refs["patched_telomere_scan_ref"] = str(out["patched_telomere_scan_ref"])
    for key in ("readback_validation_ref", "reconstruction_ref"):
        if out.get(key):
            report_refs[key] = out[key]
    notes = list(out.get("notes") or [])
    if out.get("reconstruction_ref"):
        notes.append(
            "Validation binds the complete candidate. Accept all listed candidates "
            "together, or continue with the input. Partial adoption is unavailable."
        )
    payload = ReviewPayload(
        stage="t2t",
        gate_id="t2t_review",
        reason=reason,
        candidates=candidates,
        decisions_allowed=[
            "accept_selected_candidates",
            "continue_without_patch",
            "stop_pipeline",
        ],
        auto_decidable=False,
        report_refs=report_refs,
        notes=notes,
    )
    persisted_request = persist_review_request(
        state,
        stage="t2t",
        gate_id="t2t_review",
        reason=reason,
        payload=payload,
        method_name=T2T_CONTRACT_METHOD,
        artifact_type="t2t.review_request",
    )
    request_ref = persisted_request.request_ref
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    Ledger(data_dir).record_review_requested(
        session_id,
        "t2t",
        reason=reason,
        candidates=candidates,
        review_request_artifact_ref=request_ref.model_dump(mode="json"),
        auto_decidable=False,
        review_request_id=persisted_request.review_request_id,
    )
    stage_update = update_t2t_stage_output(
        state,
        {
            "status": "blocked_human",
            "review_request_artifact_ref": request_ref.model_dump(mode="json"),
            "auto_decidable": False,
        },
    )
    active_interrupt = _top_level_interrupt(
        _T2T_INTERRUPT_ADAPTER.validate_python(
            persisted_request.compact_interrupt(
                stage="t2t",
                gate_id="t2t_review",
                reason=reason,
            )
        )
    )
    return {
        "current_stage": "t2t",
        "supervision_state": "blocked_human",
        **stage_update,
        "active_interrupt": active_interrupt,
    }


def _report_markdown(
    out: T2TStageCursor,
    summary: list[dict[str, str]],
    *,
    status: T2TStatus,
    output_assembly_ref: str,
    evidence_phase: Literal["pre_review", "final"],
) -> str:
    missing = sum(row["status"] == "missing_signal" for row in summary)
    long_read_inputs = list(out.get("long_read_inputs") or [])
    long_read_summary = ", ".join(
        f"{item['path']} [{item['platform']}]" for item in long_read_inputs
    )
    lines = [
        "# T2T finishing report",
        "",
        f"- evidence phase: `{evidence_phase}`",
        f"- status: `{status}`",
        f"- input assembly: `{out.get('input_assembly_ref', '')}`",
        f"- source stage: `{out.get('source_stage', '')}`",
        f"- long reads: `{long_read_summary or 'not available'}`",
        f"- execution enabled: `{out.get('execution_enabled')}`",
        f"- resolved telomere motif: `{out.get('resolved_telomere_motif')}`",
        f"- motif source: `{out.get('resolved_telomere_motif_source')}`",
        f"- sequences with a missing terminal signal: `{missing}`",
        f"- telomeric reads: `{out.get('telomeric_read_count', 0)}`",
        f"- extension candidates: `{out.get('candidate_count', 0)}`",
        f"- selected patches: `{out.get('selected_patch_count', 0)}`",
        f"- candidate patched assembly: `{out.get('patched_assembly_ref') or 'none'}`",
        f"- human decision: `{out.get('decision') or 'pending_or_not_required'}`",
        f"- output assembly: `{output_assembly_ref}`",
        "",
        "## Validation scope",
        "",
        "Terminal motifs do not certify T2T completeness. POA consensus and read-back",
        "use the same input reads (in-sample evidence), not independent QV validation.",
        f"Read-back evidence: `{out.get('readback_validation_ref', 'unavailable')}`.",
        "Quality evidence is advisory under the current default-adoption policy.",
        "",
        "## Notes",
    ]
    lines.extend(f"- {note}" for note in (out.get("notes") or ["none"]))
    if out.get("skip_reason"):
        lines.append(f"- skip reason: {out['skip_reason']}")
    if out.get("patch_reason"):
        lines.append(f"- patch reason: {out['patch_reason']}")
    return "\n".join(lines) + "\n"


def _register_artifacts(
    state: TopLevelState,
    out: T2TStageCursor,
    summary_path: Path,
    report_path: Path,
) -> list[Path]:
    candidates = [
        (out.get("alignment_paf_ref"), T2T_READ_ALIGNMENT_PAF),
        (out.get("assembly_telomere_scan_ref"), T2T_ASSEMBLY_TELOMERE_SCAN),
        (out.get("reads_telomere_scan_ref"), T2T_READS_TELOMERE_SCAN),
        (out.get("readback_validation_ref"), T2T_READBACK_VALIDATION),
        (out.get("readback_paf_ref"), T2T_READ_ALIGNMENT_PAF),
        (out.get("reconstruction_ref"), "t2t.reconstruction"),
        (out.get("reconstructed_assembly_ref"), T2T_PATCHED_ASSEMBLY),
        (out.get("representative_baseline_ref"), "t2t.representative_baseline"),
        (out.get("consensus_extensions_ref"), "t2t.consensus_extensions"),
        (out.get("telomeric_reads_ref"), T2T_TELOMERIC_READS),
        (out.get("telomere_extension_candidates_ref"), T2T_EXTENSION_CANDIDATES),
        (out.get("alignment_observations_ref"), T2T_ALIGNMENT_OBSERVATIONS),
        (str(summary_path), T2T_STATUS_SUMMARY),
        (str(report_path), T2T_FINISHING_REPORT),
        (out.get("patched_assembly_ref"), T2T_PATCHED_ASSEMBLY),
        (out.get("patch_records_ref"), T2T_PATCH_RECORDS),
    ]
    candidates.extend(
        (path, T2T_ALIGNMENT_READS)
        for path in sorted((out.get("alignment_reads_fasta_refs") or {}).values())
    )
    paths: list[Path] = []
    for value, artifact_type in candidates:
        if value and Path(value).exists():
            path = Path(value)
            paths.append(path)
    return paths


_ASSEMBLY_SCAN_FIELDS = [
    "sequence",
    "length",
    "left_motif_copies",
    "right_motif_copies",
    "left_has_signal",
    "right_has_signal",
]
_READ_SCAN_FIELDS = [
    "read_id",
    "source_asset_id",
    "source_member_id",
    "source_read_id",
    "platform",
    "minimap2_preset",
    "length",
    "left_motif_copies",
    "right_motif_copies",
    "left_has_signal",
    "right_has_signal",
]
_ALIGNMENT_OBSERVATION_FIELDS = [
    "read_id",
    "target",
    "read_end",
    "target_end",
    "strand",
    "mapq",
    "overlap_bp",
    "target_distance_bp",
    "extension_length",
    "tail_motif_copies",
    "decision",
    "rejection_reasons",
    "frozen_attempt_ref",
    "execution_ref",
    "alignment_paf_ref",
]
_CANDIDATE_FIELDS = [
    "candidate_id",
    "target",
    "target_end",
    "target_start",
    "target_stop",
    "junction_coordinate",
    "target_bases_trimmed",
    "read_id",
    "source_asset_id",
    "source_read_id",
    "read_end",
    "strand",
    "mapq",
    "overlap_bp",
    "target_distance_bp",
    "extension_length",
    "query_start",
    "query_end",
    "oriented_query_start",
    "oriented_query_end",
    "supporting_reads",
    "supporting_read_ids",
    "supporting_molecules",
    "molecule_id",
    "cluster_rank",
    "platform",
    "tail_motif_copies",
    "cigar",
    "reconstruction_method",
    "reconstruction_profile",
    "construction_read_ids",
    "consensus_sequence",
    "baseline_sequence",
    "evidence_scope",
    "frozen_attempt_ref",
    "execution_ref",
    "alignment_paf_ref",
    "decision",
    "reason",
]
_PATCH_RECORD_FIELDS = [
    "candidate_id",
    "target",
    "target_end",
    "source_read_id",
    "source_asset_id",
    "original_read_id",
    "source_strand",
    "extension_length",
    "target_cut_coordinate",
    "target_bases_removed",
    "bases_added",
    "supporting_reads",
    "mapq",
    "overlap_bp",
    "reconstruction_method",
    "construction_read_ids",
    "evidence_scope",
    "materialization_status",
    "reason",
]
