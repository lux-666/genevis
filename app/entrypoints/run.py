"""Command-line entrypoint and frozen-session executor for gene-agent.

Structured workflows start deterministically with ``gene-agent run``.  The TUI
remains an optional conversational intake/planning boundary; both entry paths
publish the same immutable input contract and use the same frozen executor.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml
from langchain_core.runnables import RunnableConfig

from app.config import default_data_root
from app.control.lifecycle import finalize_terminal_state
from app.domains.assembly.selection import (
    AssemblySelection,
    default_primary_method,
    resolve_read_type,
)
from app.graph.builder import build_graph
from app.graph.checkpointer import (
    DEFAULT_CHECKPOINTER_BACKEND,
    CheckpointerBackend,
)
from app.schemas.run_config import (
    AssemblyRunConfig,
    ReviewPolicyStage,
    infer_assembly_method,
)
from app.schemas.shared_parameters import SharedParameters
from app.schemas.state import DomainStage, initial_state
from app.storage.ledger import Ledger
from app.storage.paths import DataLayoutError, SessionPaths, locate_session_data_dir

if TYPE_CHECKING:
    from app.review.base import ResumeReviewRequest
    from app.storage.frozen_inputs import FrozenInputBundle


_ASSEMBLY_METHOD_NAMES = {
    "flye": "assembly.candidate.flye",
    "nextdenovo": "assembly.candidate.nextdenovo",
    "hifiasm": "assembly.candidate.hifiasm",
    "hifiasm_ont": "assembly.candidate.hifiasm_ont",
}
_ASSEMBLY_PARAMETER_ATTRS = {
    "flye": "flye_parameters",
    "nextdenovo": "nextdenovo_parameters",
    "hifiasm": "hifiasm_parameters",
    "hifiasm_ont": "hifiasm_ont_parameters",
}


def _assembly_parameter_dump(
    assembly: AssemblyRunConfig,
    method: str,
) -> dict[str, Any] | None:
    parameters = getattr(assembly, _ASSEMBLY_PARAMETER_ATTRS[method])
    return (
        parameters.model_dump(exclude_none=True, exclude_defaults=True)
        if parameters
        else None
    )


def _build_assembly_selection(
    *,
    assembly: AssemblyRunConfig,
    shared_parameters: SharedParameters | dict[str, Any] | None,
    shared_platform: Any = None,
) -> AssemblySelection | None:
    shared_values = (
        shared_parameters.model_dump(exclude_none=True)
        if isinstance(shared_parameters, SharedParameters)
        else (shared_parameters or {})
    )
    assembly_method = infer_assembly_method(assembly)
    if assembly_method:
        read_type = resolve_read_type(
            ("assembly.assembly_route", assembly.assembly_route),
            ("shared_parameters.read_type", shared_values.get("read_type")),
            ("shared_parameters.platform", None, shared_platform),
            context="entrypoints.run.assembly_selection",
        )
        if read_type is None:
            if assembly.assembly_method is not None:
                raise ValueError(
                    "run_config: assembly.assembly_method is set but no "
                    "read_type could be inferred; "
                    "set assembly.assembly_route, shared_parameters.read_type, or "
                    "shared_parameters.platform"
                )
            return None
        return AssemblySelection(
            read_type=read_type,
            primary_method=_ASSEMBLY_METHOD_NAMES[assembly_method],
            source="run_config",
            rationale="run_config explicitly selected assembly method",
            metadata={"assembly_route": assembly.assembly_route},
        )

    if assembly.assembly_route == "auto":
        return None

    read_type = resolve_read_type(
        ("assembly.assembly_route", assembly.assembly_route),
        context="entrypoints.run.assembly_route",
    )
    if read_type is None:
        return None
    return AssemblySelection(
        read_type=read_type,
        primary_method=default_primary_method(read_type),
        source="run_config",
        rationale="run_config provided assembly_route read-type hint",
        metadata={"assembly_route": assembly.assembly_route},
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gene-agent",
        description=(
            "Start deterministic workflows with 'gene-agent run', or use "
            "'gene-agent tui' for conversational intake and planning."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{run,tui,bootstrap,inspect,storage-audit,resume,review}",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Start a deterministic workflow from structured input files",
    )
    run_parser.add_argument(
        "--manifest",
        required=True,
        help="Path to the input manifest YAML/JSON file",
    )
    run_parser.add_argument(
        "--run-config",
        dest="run_config",
        required=True,
        help="Path to the deterministic run-config JSON file",
    )
    run_parser.add_argument(
        "--data-dir",
        required=True,
        help="Workflow data-layout root; outputs land below <root>/runs/<session_id>",
    )
    run_parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=False,
        help="Persist a human gate and exit instead of prompting",
    )

    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect a workflow session from durable records"
    )
    inspect_parser.add_argument(
        "--session-id", required=True, help="Session identifier to inspect"
    )
    inspect_parser.add_argument(
        "--data-dir", default=default_data_root(), help="Workflow data root"
    )
    inspect_parser.add_argument(
        "--filter",
        action="append",
        default=[],
        help="Filter inspect output, currently supports decided_by=<origin>",
    )
    inspect_parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable inspect summary"
    )
    inspect_parser.add_argument(
        "--trace",
        action="store_true",
        default=False,
        help="Generate and register a stage-by-stage run trace from durable records",
    )
    inspect_parser.add_argument(
        "--write-decision-template",
        help="Write a JSON decision-file template for the active review gate",
    )

    storage_audit_parser = subparsers.add_parser(
        "storage-audit",
        help="Audit one v2 run's storage without changing it",
    )
    storage_audit_parser.add_argument(
        "--session-id", required=True, help="Session identifier to audit"
    )
    storage_audit_parser.add_argument(
        "--data-dir", default=default_data_root(), help="Workflow data root"
    )
    storage_audit_parser.add_argument(
        "--format",
        choices=["json"],
        default="json",
        help="Machine-readable output format",
    )
    storage_audit_parser.add_argument(
        "--dry-run-manifest",
        action="store_true",
        default=False,
        help="Include a report-only cleanup manifest; never deletes files",
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume a blocked workflow session from a structured decision file",
    )
    resume_parser.add_argument(
        "--session-id", required=True, help="Session identifier to resume"
    )
    resume_parser.add_argument(
        "--decision-file",
        dest="decision_file",
        required=True,
        help="Path to a JSON decision file matching ResumeReviewRequest",
    )
    resume_parser.add_argument(
        "--data-dir", default=default_data_root(), help="Workflow data root"
    )
    resume_parser.add_argument(
        "--max-blocked-seconds",
        type=float,
        default=None,
        help=(
            "Reject/convert a blocked resume if the gate has been blocked for "
            "at least this many seconds"
        ),
    )
    resume_parser.add_argument(
        "--checkpointer",
        choices=["sqlite", "memory"],
        default=None,
        help=(
            "Checkpoint backend; must match what the run used. Falls "
            "back to GENE_AGENT_CHECKPOINTER env var, then 'sqlite'."
        ),
    )

    review_parser = subparsers.add_parser(
        "review",
        help="Interactively review and resume a blocked human gate",
    )
    review_parser.add_argument(
        "--session-id", required=True, help="Session identifier to review"
    )
    review_parser.add_argument(
        "--data-dir", default=default_data_root(), help="Workflow data root"
    )
    review_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate and write decision artifact without resuming",
    )
    review_parser.add_argument(
        "--checkpointer",
        choices=["sqlite", "memory"],
        default="sqlite",
        help="Checkpoint backend for interactive review; review requires sqlite.",
    )

    tui_parser = subparsers.add_parser(
        "tui",
        help="Start, run, or resume an interactive workflow",
    )
    tui_parser.add_argument(
        "--data-dir", default=default_data_root(), help="Workflow data root"
    )
    tui_parser.add_argument(
        "--resume-session",
        default=None,
        metavar="SESSION_ID",
        help=argparse.SUPPRESS,
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help=(
            "Auxiliary machine configuration initialization and validation"
        ),
    )
    bootstrap_mode = bootstrap_parser.add_mutually_exclusive_group()
    bootstrap_mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply planned project configuration and package conformance actions.",
    )
    bootstrap_mode.add_argument(
        "--update",
        action="store_true",
        default=False,
        help=(
            "Select existing bootstrap-managed fields to replace; default mode "
            "only fills missing values."
        ),
    )
    bootstrap_parser.add_argument(
        "--config",
        default=None,
        help="Path to envs.yaml (default: config/envs.yaml)",
    )

    return parser.parse_args(argv)


def _resolve_checkpointer_backend(args: argparse.Namespace) -> CheckpointerBackend:
    """Resolve the checkpoint backend with explicit > env var > default.

    Frozen-session execution and ``gene-agent resume`` default to durable
    sqlite while tests may explicitly select the in-memory backend.
    """
    configured = (
        getattr(args, "checkpointer", None)
        or os.environ.get("GENE_AGENT_CHECKPOINTER")
        or DEFAULT_CHECKPOINTER_BACKEND
    )
    if configured not in {"sqlite", "memory"}:
        raise ValueError(f"unsupported checkpointer backend: {configured!r}")
    return cast(CheckpointerBackend, configured)


def _resolve_data_dir(data_dir: str) -> str:
    """Resolve CLI data_dir once so generated stage scripts get stable roots."""
    return str(Path(data_dir).expanduser().resolve(strict=False))


def _run(args: argparse.Namespace) -> int:
    """Preflight, freeze, and execute one deterministic structured run."""

    from app.storage.frozen_inputs import (
        FrozenInputError,
        FrozenInputValidationError,
        freeze_session_inputs,
        prepare_session_inputs,
    )

    try:
        if not str(args.data_dir).strip():
            raise FrozenInputValidationError("--data-dir must not be blank")
        prepared = prepare_session_inputs(
            manifest_draft=args.manifest,
            run_config_draft=args.run_config,
        )
        required_fields = {"requested_stages", "local_exec"}
        missing_fields = sorted(
            required_fields.difference(prepared.explicit_run_config_fields)
        )
        if missing_fields:
            raise FrozenInputValidationError(
                "run config must explicitly provide top-level field(s): "
                + ", ".join(missing_fields)
            )

        requested_stages = prepared.run_config.requested_stages
        if requested_stages is None:
            raise FrozenInputValidationError(
                "run config field requested_stages must be a JSON array, not null"
            )

        data_dir = _resolve_data_dir(args.data_dir)
        paths = SessionPaths(data_dir, prepared.manifest.session_id)
        freeze_session_inputs(
            paths,
            prepared_inputs=prepared,
            selected_stages=list(requested_stages),
            selected_methods={},
        )
    except (DataLayoutError, FrozenInputError, OSError, ValueError) as exc:
        print(f"error: run preflight failed: {exc}", file=sys.stderr)
        return 1

    try:
        return execute_frozen_session(
            paths.session_id,
            paths.data_root,
            checkpointer="sqlite",
            interactive_gate=not args.non_interactive,
        )
    except Exception as exc:
        print(f"error: run execution failed: {exc}", file=sys.stderr)
        return 1


def execute_frozen_session(
    session_id: str,
    data_dir: str | Path,
    *,
    checkpointer: CheckpointerBackend | None = None,
    interactive_gate: bool = True,
) -> int:
    """Execute one verified frozen session without rematerializing inputs.

    Both public ``run`` and the TUI call this boundary.  It intentionally
    accepts only session identity and data root: the freeze marker is the sole
    authority for manifest, run config, and dependency inputs.
    """

    from app.storage.frozen_inputs import (
        FrozenExecutionActiveError,
        FrozenExecutionRecoveryError,
        FrozenInputError,
        frozen_execution_lock,
        frozen_execution_progress,
        validate_frozen_inputs,
    )

    try:
        paths = SessionPaths(data_dir, session_id)
        with frozen_execution_lock(paths):
            bundle = validate_frozen_inputs(paths)
            progress = frozen_execution_progress(paths)
            if progress == "partial":
                raise FrozenExecutionRecoveryError(
                    "partial execution evidence exists without a resumable checkpoint"
                )

            return _execute_verified_frozen_session(
                bundle,
                checkpointer=checkpointer,
                resume_checkpoint=progress == "checkpoint",
                interactive_gate=interactive_gate,
            )
    except FrozenExecutionActiveError as exc:
        print(f"error: frozen session is already active: {exc}", file=sys.stderr)
        return 1
    except (DataLayoutError, FrozenInputError, OSError, ValueError) as exc:
        print(f"error: frozen session cannot execute: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: frozen session cannot execute: {exc}", file=sys.stderr)
        return 1


def _execute_verified_frozen_session(
    bundle: "FrozenInputBundle",
    *,
    checkpointer: CheckpointerBackend | None,
    resume_checkpoint: bool,
    interactive_gate: bool,
) -> int:
    """Seed and execute a graph from one already-verified frozen bundle."""

    configured_backend = (
        checkpointer
        or os.environ.get("GENE_AGENT_CHECKPOINTER")
        or DEFAULT_CHECKPOINTER_BACKEND
    )
    if configured_backend not in {"sqlite", "memory"}:
        print(
            f"error: unsupported checkpointer backend: {configured_backend!r}",
            file=sys.stderr,
        )
        return 1
    backend = cast(CheckpointerBackend, configured_backend)

    paths = bundle.paths
    session_id = paths.session_id
    run_config = bundle.run_config
    run_dir = str(paths.run_dir)
    manifest_ref = paths.to_ref(paths.input_manifest_path)
    Ledger(run_dir).create_session(session_id, manifest_ref)

    requested_stages = (
        list(run_config.requested_stages)
        if run_config.requested_stages is not None
        else None
    )
    state = initial_state(
        session_id=session_id,
        input_manifest_ref=manifest_ref,
        data_dir=run_dir,
        requested_stages=(
            [cast(DomainStage, stage) for stage in requested_stages]
            if requested_stages is not None
            else None
        ),
    )

    local_stages = list(run_config.local_exec)
    stage_outputs = dict(state.get("stage_outputs") or {})
    if local_stages:
        for stage_name in local_stages:
            existing = dict(stage_outputs.get(stage_name) or {})
            existing["execution_mode"] = "local"
            stage_outputs[stage_name] = existing
    if run_config.shared_parameters:
        state["shared_parameters"] = run_config.shared_parameters.model_dump(
            exclude_none=True
        )
    if run_config.read_qc and run_config.read_qc.length_filter:
        read_qc_out = dict(stage_outputs.get("read_qc") or {})
        read_qc_out["length_filter_request"] = (
            run_config.read_qc.length_filter.model_dump(mode="json")
        )
        stage_outputs["read_qc"] = read_qc_out
    if run_config.genome_survey and run_config.genome_survey.survey_parameters:
        survey_out = dict(stage_outputs.get("genome_survey") or {})
        survey_out["survey_parameters"] = (
            run_config.genome_survey.survey_parameters.model_dump(exclude_none=True)
        )
        stage_outputs["genome_survey"] = survey_out
    if "repeat_annotation" in local_stages:
        repeat_out = dict(stage_outputs.get("repeat_annotation") or {})
        repeat_out.setdefault(
            "primary_method", "repeat_annotation.masking.repeatmasker"
        )
        stage_outputs["repeat_annotation"] = repeat_out
    if run_config.assembly:
        assembly_out = dict(stage_outputs.get("assembly") or {})
        try:
            selection = _build_assembly_selection(
                assembly=run_config.assembly,
                shared_parameters=state.get("shared_parameters"),
                shared_platform=(
                    run_config.shared_parameters.platform
                    if run_config.shared_parameters
                    else None
                ),
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if selection:
            assembly_out["assembly_selection"] = selection.model_dump(mode="json")
        if run_config.assembly.flye_parameters:
            assembly_out["flye_parameters"] = _assembly_parameter_dump(
                run_config.assembly, "flye"
            )
        if run_config.assembly.nextdenovo_parameters:
            assembly_out["nextdenovo_parameters"] = _assembly_parameter_dump(
                run_config.assembly, "nextdenovo"
            )
        if run_config.assembly.hifiasm_parameters:
            assembly_out["hifiasm_parameters"] = _assembly_parameter_dump(
                run_config.assembly, "hifiasm"
            )
        if run_config.assembly.hifiasm_ont_parameters:
            assembly_out["hifiasm_ont_parameters"] = _assembly_parameter_dump(
                run_config.assembly, "hifiasm_ont"
            )
        for key in [
            "hifi_reads_ref",
            "hic_reads_ref",
            "target_level",
            "assembly_route",
        ]:
            value = getattr(run_config.assembly, key)
            if value is not None:
                assembly_out[key] = value
        if assembly_out:
            stage_outputs["assembly"] = assembly_out
    if run_config.assembly_review:
        review_out = dict(stage_outputs.get("assembly_review") or {})
        if run_config.assembly_review.quast:
            review_out["quast_parameters"] = (
                run_config.assembly_review.quast.model_dump()
            )
        if run_config.assembly_review.busco:
            review_out["busco_parameters"] = (
                run_config.assembly_review.busco.model_dump()
            )
        review_out["max_reruns"] = run_config.assembly_review.max_reruns
        if run_config.assembly_review.related_species_reference_dir:
            review_out["related_species_reference_dir"] = (
                run_config.assembly_review.related_species_reference_dir
            )
        stage_outputs["assembly_review"] = review_out
    if run_config.hic_scaffolding:
        hic_out = dict(stage_outputs.get("hic_scaffolding") or {})
        if run_config.hic_scaffolding.autohic_parameters:
            hic_out["autohic_parameters"] = (
                run_config.hic_scaffolding.autohic_parameters.model_dump(
                    exclude_none=True
                )
            )
        hic_out["parameter_retry_policy"] = {
            "retry_on_execution_failure": (
                run_config.hic_scaffolding.retry_on_execution_failure
            ),
            "max_execution_retries": run_config.hic_scaffolding.max_execution_retries,
            "max_review_retries": run_config.hic_scaffolding.max_review_retries,
        }
        stage_outputs["hic_scaffolding"] = hic_out
    if run_config.t2t:
        t2t_out = dict(stage_outputs.get("t2t") or {})
        t2t_out.update(run_config.t2t.model_dump(mode="json", exclude_none=True))
        stage_outputs["t2t"] = t2t_out
    if run_config.review_policy:
        review_stages: tuple[ReviewPolicyStage, ...] = (
            "assembly",
            "assembly_review",
            "hic_scaffolding",
        )
        for review_stage in review_stages:
            stage_out = dict(stage_outputs.get(review_stage) or {})
            stage_out["review_policy"] = run_config.review_policy.effective_for_stage(
                review_stage
            )
            stage_outputs[review_stage] = stage_out
    if (
        run_config.gene_annotation
        and run_config.gene_annotation.lineage_or_species_hints
    ):
        gene_out = dict(stage_outputs.get("gene_annotation") or {})
        gene_out["lineage_or_species_hints"] = dict(
            run_config.gene_annotation.lineage_or_species_hints
        )
        stage_outputs["gene_annotation"] = gene_out
    if (
        run_config.function_annotation
        and run_config.function_annotation.database_provenance
    ):
        fn_out = dict(stage_outputs.get("function_annotation") or {})
        fn_out["database_provenance"] = [
            p.model_dump(mode="json")
            for p in run_config.function_annotation.database_provenance
        ]
        stage_outputs["function_annotation"] = fn_out
    if stage_outputs != (state.get("stage_outputs") or {}):
        state["stage_outputs"] = stage_outputs

    graph = build_graph(backend=backend, data_dir=run_dir)
    config: RunnableConfig = {"configurable": {"thread_id": session_id}}
    try:
        result = graph.invoke(None if resume_checkpoint else state, config=config)
    except Exception as exc:
        # A stage bug must still leave an operator-readable durable terminal
        # record.  Prefer the latest checkpoint values because the in-memory
        # initial state may be several stages behind the node that raised.
        latest_state = dict(state)
        try:
            snapshot = graph.get_state(config)
            snapshot_values = getattr(snapshot, "values", None)
            if isinstance(snapshot_values, dict):
                latest_state.update(snapshot_values)
        except Exception:
            # Failure reporting must not hide the original graph exception when
            # a backend cannot expose a checkpoint snapshot.
            pass
        error = f"{type(exc).__name__}: {exc}"
        failure_result = {
            **latest_state,
            "session_id": session_id,
            "data_dir": run_dir,
            "input_manifest_ref": manifest_ref,
            "supervision_state": "failed_terminal",
            "last_error": error,
            "checkpoint_persistence": backend,
        }
        compaction_warning = finalize_terminal_state(
            graph,
            config,
            failure_result,
            session_id,
            run_dir,
            backend,
            report_kind="operator",
        )
        if compaction_warning:
            print(
                "error: failed to persist/compact terminal failure checkpoint: "
                f"{compaction_warning}",
                file=sys.stderr,
            )
        print(f"error: workflow invoke failed: {error}", file=sys.stderr)
        return 1

    supervision = result.get("supervision_state", "unknown")

    terminal_finalized_by_resume = False
    if supervision == "blocked_human" and interactive_gate and backend == "sqlite":
        from app.control.resume_transaction import ResumeTransactionError

        try:
            result = _interactive_gate_loop(
                graph,
                config,
                result,
                session_id,
                run_dir,
            )
        except ResumeTransactionError as exc:
            print(
                f"error: resume {exc.phase} failed: {exc}",
                file=sys.stderr,
            )
            return 1
        supervision = result.get("supervision_state", "unknown")
        terminal_finalized_by_resume = supervision in {"completed", "failed_terminal"}

    current_stage = result.get("current_stage", "unknown")
    route_trace = result.get("route_trace", [])
    registry_ref = result.get("asset_registry_ref")
    stage_plan_ref = result.get("stage_plan_ref")
    active_interrupt = result.get("active_interrupt")

    print(f"session_id: {session_id}")
    print(f"current_stage: {current_stage}")
    print(f"supervision_state: {supervision}")
    print(f"checkpoint_persistence: {backend}")
    if supervision == "blocked_human":
        if backend == "sqlite":
            if interactive_gate:
                print(
                    "resume_note: use `gene-agent review` or `gene-agent resume` "
                    "to continue"
                )
            else:
                _print_noninteractive_gate_guidance(paths)
        else:
            print(
                "resume_note: durable cross-process resume requires "
                "--checkpointer sqlite"
            )
    print(f"route_trace: {route_trace}")
    if active_interrupt:
        print(f"active_interrupt: {active_interrupt}")
    if registry_ref:
        print(f"asset_registry_ref: {registry_ref}")
    if stage_plan_ref:
        print(f"stage_plan_ref: {stage_plan_ref}")
    print(f"run_config_ref: {paths.run_config_path.resolve(strict=False)}")

    compaction_warning = None
    if not terminal_finalized_by_resume:
        compaction_warning = finalize_terminal_state(
            graph,
            config,
            result,
            session_id,
            run_dir,
            backend,
            report_kind="final",
        )
    if compaction_warning:
        print(
            f"warning: terminal checkpoint compaction skipped: {compaction_warning}",
            file=sys.stderr,
        )

    if supervision == "failed_terminal":
        print(f"error: {result.get('last_error', 'unknown failure')}", file=sys.stderr)
        return 1

    return 0


def _print_noninteractive_gate_guidance(paths: SessionPaths) -> None:
    """Print shell-copyable continuation commands for one durable human gate."""

    session_id = paths.session_id
    data_root = str(paths.data_root)
    decision_file = str(paths.data_root / "decisions" / f"{session_id}_decision.json")
    commands = {
        "inspect_command": [
            "gene-agent",
            "inspect",
            "--session-id",
            session_id,
            "--data-dir",
            data_root,
            "--write-decision-template",
            decision_file,
        ],
        "review_command": [
            "gene-agent",
            "review",
            "--session-id",
            session_id,
            "--data-dir",
            data_root,
            "--checkpointer",
            "sqlite",
        ],
        "resume_command": [
            "gene-agent",
            "resume",
            "--session-id",
            session_id,
            "--decision-file",
            decision_file,
            "--data-dir",
            data_root,
            "--checkpointer",
            "sqlite",
        ],
    }
    print("resume_note: workflow is safely blocked; continue with one command below")
    for label, command in commands.items():
        print(f"{label}: {shlex.join(command)}")


def _interactive_gate_loop(
    graph: Any,
    config: RunnableConfig,
    result: dict[str, Any],
    session_id: str,
    data_dir: str,
) -> dict[str, Any]:
    """Loop on human gates: prompt operator, resume, repeat until not blocked."""
    from app.control.resume_transaction import (
        ResumeTransactionError,
        execute_resume_transaction,
    )
    from app.entrypoints.review_interactive import collect_gate_decision
    from app.review.annotation_intake import collect_annotation_intake_selection

    while result.get("supervision_state") == "blocked_human":
        gate_result = collect_gate_decision(session_id, data_dir)
        if gate_result is None:
            print("review skipped; session remains blocked")
            break

        request, _raw_decision = gate_result

        try:
            transaction = execute_resume_transaction(
                graph=graph,
                config=config,
                session_id=session_id,
                data_dir=data_dir,
                request=request,
                source="interactive_gate",
                checkpointer="sqlite",
                annotation_intake_collector=collect_annotation_intake_selection,
            )
        except ResumeTransactionError as exc:
            print(
                f"error: resume {exc.phase} failed: {exc}",
                file=sys.stderr,
            )
            break

        result = transaction.state
        if transaction.decision_artifact_path is not None:
            print(f"decision_artifact: {transaction.decision_artifact_path}")
        if transaction.annotation_intake_artifact_ref is not None:
            print(
                f"annotation_intake_plan: {transaction.annotation_intake_artifact_ref}"
            )
        print(
            f"\nresumed → stage={result.get('current_stage')} "
            f"state={result.get('supervision_state')}"
        )

    return result


def generate_session_id(
    data_dir: str | Path | None = None, *, now: datetime | None = None
) -> str:
    date = (now or datetime.now()).strftime("%Y%m%d")
    root = Path(data_dir or default_data_root())
    pattern = re.compile(rf"^gene-{date}-(\d{{6}})$")
    max_index = 0
    session_root = root / "runs"
    if session_root.exists():
        for child in session_root.iterdir():
            if child.is_dir():
                match = pattern.match(child.name)
                if match:
                    max_index = max(max_index, int(match.group(1)))
    return f"gene-{date}-{max_index + 1:06d}"


def _run_storage_audit(args: argparse.Namespace) -> int:
    """Emit one read-only v2 storage audit and optional dry-run manifest."""

    from app.storage.audit import audit_run_storage, build_cleanup_manifest

    data_root = _resolve_data_dir(args.data_dir)
    run_root = SessionPaths(data_root, args.session_id).run_dir
    try:
        audit = audit_run_storage(run_root)
        payload: dict[str, Any] = audit
        if getattr(args, "dry_run_manifest", False):
            payload = {
                "audit": audit,
                "cleanup_manifest": build_cleanup_manifest(audit),
            }
    except (DataLayoutError, OSError, ValueError) as exc:
        print(f"error: storage audit failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _run_inspect(args: argparse.Namespace) -> int:
    from app.control.errors import ResumeValidationError
    from app.control.lifecycle import (
        build_resume_decision_template,
        inspect_session,
    )

    filters = _parse_inspect_filters(getattr(args, "filter", []) or [])
    if filters is None:
        return 1
    data_root = _resolve_data_dir(args.data_dir)
    data_dir = locate_session_data_dir(data_root, args.session_id)
    if data_dir is None:
        data_dir = SessionPaths(data_root, args.session_id).run_dir
    summary = inspect_session(args.session_id, data_dir)
    template_path = getattr(args, "write_decision_template", None)
    if template_path:
        try:
            template = build_resume_decision_template(summary)
        except ResumeValidationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        out_path = Path(template_path).expanduser().resolve(strict=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        resume_command = (
            "gene-agent resume "
            f"--session-id {args.session_id} --data-dir {args.data_dir} "
            f"--decision-file {out_path} --checkpointer sqlite"
        )
        summary["decision_template_written"] = str(out_path)
        summary["decision_template_resume_command"] = resume_command
        if not getattr(args, "json", False):
            print(f"decision_template: {out_path}")
            print(f"resume_command: {resume_command}")

    trace_markdown: str | None = None
    if getattr(args, "trace", False):
        from app.reports.trace_writer import write_session_trace

        trace_result = write_session_trace(data_dir, args.session_id)
        summary["trace"] = trace_result["trace"]
        summary["trace_json_ref"] = trace_result["json_ref"]
        summary["trace_markdown_ref"] = trace_result["markdown_ref"]
        trace_markdown = trace_result["markdown"]

    if getattr(args, "json", False):
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        _print_inspect_text(summary, filters)
        if trace_markdown is not None:
            if summary.get("trace_json_ref"):
                print(f"trace_json: {summary['trace_json_ref']}")
                print(f"trace_markdown: {summary['trace_markdown_ref']}")
            print()
            print(trace_markdown)
    if summary.get("error"):
        print(f"error: {summary['error']}", file=sys.stderr)
        return 1
    return 0


def _print_inspect_text(
    summary: dict[str, Any],
    filters: dict[str, str],
) -> None:
    print(f"session_id: {summary['session_id']}")
    print(f"supervision_state: {summary['supervision_state']}")
    print(f"checkpoint_persistence: {summary['checkpoint_persistence']}")
    print(
        f"durable_resume_available: {str(summary['durable_resume_available']).lower()}"
    )
    print(f"artifact_count: {summary.get('artifact_count', 0)}")
    print(f"decisions_by_origin: {summary.get('decisions_by_origin', {})}")
    if summary.get("latest_review_request"):
        print(f"latest_review_request: {summary['latest_review_request']}")
    if summary.get("latest_review_request_artifact"):
        print(
            "latest_review_request_artifact: "
            f"{summary['latest_review_request_artifact']}"
        )
    if summary.get("latest_operator_report"):
        print(f"latest_operator_report: {summary['latest_operator_report']}")
    if summary.get("latest_final_report"):
        print(f"latest_final_report: {summary['latest_final_report']}")
    requirements = summary.get("decision_requirements")
    if requirements:
        print(f"decision_requirements.stage: {requirements.get('stage')}")
        decisions_allowed = requirements.get("decisions_allowed") or []
        print(f"decision_requirements.decisions_allowed: {decisions_allowed}")
        if requirements.get("candidate_ids"):
            print(
                f"decision_requirements.candidate_ids: {requirements['candidate_ids']}"
            )
        if requirements.get("candidate_methods"):
            print(
                "decision_requirements.candidate_methods: "
                f"{requirements['candidate_methods']}"
            )
    if summary.get("latest_review_decision"):
        print(f"latest_review_decision: {summary['latest_review_decision']}")
    review_decisions = _filter_review_decisions(
        summary.get("review_decisions") or [], filters
    )
    if filters:
        print(f"review_decisions: {review_decisions}")
    next_action = summary.get("next_action") or {}
    print(f"next_action.kind: {next_action.get('kind')}")
    print(f"next_action.summary: {next_action.get('summary')}")
    if next_action.get("required_fields"):
        print(f"next_action.required_fields: {next_action['required_fields']}")
    if next_action.get("command"):
        print(f"next_action.command: {next_action['command']}")
    if next_action.get("resume_command"):
        print(f"next_action.resume_command: {next_action['resume_command']}")
    if next_action.get("relevant_refs"):
        print(f"next_action.relevant_refs: {next_action['relevant_refs']}")


def _parse_inspect_filters(raw_filters: list[str]) -> dict[str, str] | None:
    parsed: dict[str, str] = {}
    for raw_filter in raw_filters:
        if "=" not in raw_filter:
            print(
                f"error: unsupported inspect filter {raw_filter!r}; expected key=value",
                file=sys.stderr,
            )
            return None
        key, value = raw_filter.split("=", 1)
        if key != "decided_by":
            print(
                f"error: unsupported inspect filter key {key!r}; "
                "only decided_by is supported",
                file=sys.stderr,
            )
            return None
        parsed[key] = value
    return parsed


def _filter_review_decisions(
    decisions: list[dict[str, Any]],
    filters: dict[str, str],
) -> list[dict[str, Any]]:
    decided_by = filters.get("decided_by")
    if not decided_by:
        return decisions
    return [
        decision for decision in decisions if decision.get("decided_by") == decided_by
    ]


def _load_dotenv_without_override(paths: list[Path] | None = None) -> None:
    for path in paths if paths is not None else _candidate_dotenv_paths():
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return


def _candidate_dotenv_paths() -> list[Path]:
    cwd = Path(os.getcwd()).resolve()
    repo_root = Path(__file__).resolve().parents[2]
    return list(dict.fromkeys([cwd / ".env", repo_root / ".env"]))


def _snapshot_resume_decision(
    source: Path,
    paths: SessionPaths,
    decision: "ResumeReviewRequest",
) -> Path:
    """Return a run-local decision path, snapshotting external input once."""
    source = source.expanduser().resolve(strict=False)
    try:
        source.relative_to(paths.run_dir)
    except ValueError:
        source_bytes = source.read_bytes()
        token = str(
            getattr(decision, "review_request_id", None)
            or getattr(decision, "review_request_hash", None)
            or "decision"
        )
        token = re.sub(r"[^A-Za-z0-9_.-]+", "_", token).strip("_") or "decision"
        digest = hashlib.sha256(source_bytes).hexdigest()[:16]
        destination = paths.control_dir / "resume_decisions" / f"{token}.{digest}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != source_bytes:
                raise ValueError(
                    "run-local resume decision already exists with different "
                    f"content: {destination}"
                )
        else:
            shutil.copy2(source, destination)
        return destination
    return source


def _run_resume(args: argparse.Namespace) -> int:
    from app.control.resume_transaction import (
        ResumeTransactionError,
        execute_resume_transaction,
    )
    from app.review.base import load_resume_review_request

    backend = _resolve_checkpointer_backend(args)
    if backend != "sqlite":
        print(
            "error: durable resume requires --checkpointer sqlite; "
            "memory checkpoints do not survive between processes",
            file=sys.stderr,
        )
        return 1

    try:
        decision = load_resume_review_request(args.decision_file)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if decision.session_id != args.session_id:
        print(
            "error: decision session_id "
            f"{decision.session_id!r} does not match CLI --session-id "
            f"{args.session_id!r}",
            file=sys.stderr,
        )
        return 1

    # The control-layer resume transaction re-checks that
    # state.session_id == decision.session_id. That is intentional
    # defense-in-depth: the helper is also called directly by the
    # in-process resume tests in `tests/test_review_resume.py`, where
    # this CLI guard does not run. Keep both checks in place.

    from app.graph.checkpointer import checkpoint_db_path

    data_root = _resolve_data_dir(args.data_dir)
    data_dir = locate_session_data_dir(data_root, args.session_id)
    if data_dir is None:
        data_dir = SessionPaths(data_root, args.session_id).run_dir
    db_path = checkpoint_db_path(data_dir)
    if not db_path.exists():
        print(
            f"error: no durable checkpoint database at {db_path}; "
            "check that --data-dir matches the original workflow run, "
            "or run the workflow with --checkpointer sqlite before resuming",
            file=sys.stderr,
        )
        return 1

    graph = build_graph(backend=backend, data_dir=str(data_dir))
    config: RunnableConfig = {"configurable": {"thread_id": args.session_id}}
    try:
        paths = SessionPaths.from_run_dir(data_dir)
        decision_path = _snapshot_resume_decision(
            Path(args.decision_file), paths, decision
        )
        decision_ref = paths.to_ref(decision_path)
    except (OSError, ValueError) as exc:
        print(
            f"error: resume decision could not be materialized: {exc}", file=sys.stderr
        )
        return 1
    event_context = {
        "decision_file_ref": decision_ref,
    }
    try:
        transaction = execute_resume_transaction(
            graph=graph,
            config=config,
            session_id=args.session_id,
            data_dir=data_dir,
            request=decision,
            source="cli",
            checkpointer=backend,
            blocked_timeout_seconds=getattr(args, "max_blocked_seconds", None),
            collect_default_annotation_intake=True,
            event_context=event_context,
        )
    except ResumeTransactionError as exc:
        if exc.phase == "validation":
            print(f"error: resume rejected: {exc}", file=sys.stderr)
        else:
            print(f"error: resume {exc.phase} failed: {exc}", file=sys.stderr)
        return 1

    result = transaction.state
    if transaction.annotation_intake_artifact_ref is not None:
        print(f"annotation_intake_plan: {transaction.annotation_intake_artifact_ref}")
    current_stage = result.get("current_stage", "unknown")
    supervision = result.get("supervision_state", "unknown")
    route_trace = result.get("route_trace", [])
    active_interrupt = result.get("active_interrupt")

    print(f"session_id: {args.session_id}")
    print(f"current_stage: {current_stage}")
    print(f"supervision_state: {supervision}")
    print(f"checkpoint_persistence: {backend}")
    print(f"route_trace: {route_trace}")
    if active_interrupt:
        print(f"active_interrupt: {active_interrupt}")

    if supervision == "failed_terminal":
        print(f"error: {result.get('last_error', 'unknown failure')}", file=sys.stderr)
        return 1
    return 0


def _run_review(args: argparse.Namespace) -> int:
    from app.entrypoints.review_interactive import run_interactive_review

    if getattr(args, "checkpointer", "sqlite") != "sqlite":
        print(
            "error: interactive review requires --checkpointer sqlite; "
            "memory checkpoints do not survive between processes",
            file=sys.stderr,
        )
        return 1
    data_root = _resolve_data_dir(args.data_dir)
    data_dir = locate_session_data_dir(data_root, args.session_id)
    if data_dir is None:
        data_dir = SessionPaths(data_root, args.session_id).run_dir
    return run_interactive_review(
        args.session_id,
        str(data_dir),
        dry_run=getattr(args, "dry_run", False),
    )


def _run_tui(args: argparse.Namespace) -> int:
    from app.entrypoints.tui import run_tui

    data_dir = _resolve_data_dir(args.data_dir)
    return run_tui(data_dir, resume_session=getattr(args, "resume_session", None))


def _run_bootstrap(args: argparse.Namespace) -> int:
    """Initialize project config, then validate and conform packages."""
    from app.execution import env_resolver
    from app.execution.method_preflight import preflight_methods
    from app.execution.package_bootstrap import (
        conform_juicer,
        preflight_all,
        verify_envs_yaml,
    )
    from app.execution.project_bootstrap import (
        apply_project_bootstrap_plan,
        collect_project_bootstrap_plan,
        validate_project_bootstrap_plan,
    )

    # Determine config path
    if args.config:
        config_path = Path(args.config)
    else:
        config_path = Path("config/envs.yaml")

    update_requested = bool(getattr(args, "update", False))
    apply_requested = bool(getattr(args, "apply", False))
    conformance_packages = {"juicer"} if apply_requested else set()
    if update_requested and apply_requested:
        print(
            "error: bootstrap --update commits interactively and cannot be "
            "combined with --apply",
            file=sys.stderr,
        )
        return 1

    print(f"Checking configuration: {config_path}")
    existing_issues = verify_envs_yaml(config_path) if config_path.exists() else []
    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
    effective_apply = apply_requested
    needs_initialization = (
        not config_path.exists()
        or interactive
        or update_requested
        or any("placeholder" in issue for issue in existing_issues)
    )
    if needs_initialization:
        try:
            init_plan = collect_project_bootstrap_plan(
                config_path,
                prompt=input,
                secret_prompt=getpass.getpass,
                update=update_requested,
            )
        except (KeyboardInterrupt, EOFError):
            print("\nBootstrap update cancelled; no changes were written.")
            return 1
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(
                f"\n❌ Failed to inspect project configuration: {exc}", file=sys.stderr
            )
            return 1

        if update_requested:
            conformance_packages = set(init_plan.conformance_packages)

        if init_plan.unresolved_fields:
            print("\n❌ Missing project configuration values:")
            for field in init_plan.unresolved_fields:
                print(f"  - {field}")
            return 1
        try:
            validate_project_bootstrap_plan(init_plan)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(f"\n❌ Candidate configuration is invalid: {exc}", file=sys.stderr)
            return 1

        if init_plan.changes:
            print("\nProject configuration plan:")
            for change in init_plan.changes:
                if update_requested:
                    print(
                        f"  - {change.file}: {change.field} "
                        f"{change.display_previous_value()} -> "
                        f"{change.display_value()}"
                    )
                elif change.previous_value is None:
                    print(
                        f"  - {change.file}: {change.field} "
                        f"= {change.display_value()}"
                    )
                else:
                    print(
                        f"  - {change.file}: {change.field} "
                        f"{change.display_previous_value()} -> "
                        f"{change.display_value()}"
                    )
        print("✓ Candidate configuration is valid")
        if init_plan.has_changes:
            if update_requested:
                while True:
                    try:
                        confirmation = input(
                            "\nPress Enter to apply these changes "
                            "(Ctrl-C/Ctrl-D cancels): "
                        )
                    except (KeyboardInterrupt, EOFError):
                        print("\nBootstrap update cancelled; no changes were written.")
                        return 1
                    if confirmation == "":
                        effective_apply = True
                        break
                    print(
                        "Type nothing and press Enter to apply, or interrupt "
                        "to cancel."
                    )
            elif not apply_requested:
                print("\nRun with --apply to write these project configuration changes")
                return 0
            try:
                apply_project_bootstrap_plan(init_plan)
                _load_dotenv_without_override([init_plan.dotenv_path])
            except (OSError, ValueError, yaml.YAMLError) as exc:
                print(
                    f"\n❌ Failed to initialize project configuration: {exc}",
                    file=sys.stderr,
                )
                return 1
            print("✓ Project configuration initialized")

    # Validate the persisted candidate, allowing only canaries produced by the
    # declared conformance action that runs immediately below.
    issues = verify_envs_yaml(
        config_path,
        conformance_packages=conformance_packages,
    )
    if issues:
        print("\n❌ Configuration issues found:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("✓ Configuration file is valid")

    initial_issues = list(issues)

    # Step 2: Load resolver
    try:
        resolver = env_resolver.EnvResolver(config_path)
    except Exception as e:
        print(f"\n❌ Failed to load configuration: {e}", file=sys.stderr)
        return 1

    # Step 3: Run conformance for packages that need it (Juicer)
    conformance_results = []
    for package_name, package_spec in resolver.packages.items():
        if package_spec.conform_on_bootstrap:
            if update_requested and package_name not in conformance_packages:
                continue
            print(f"\nConformance: {package_name}")
            if package_name == "juicer":
                # Get juicer_tools spec if referenced
                jar_root_ref = package_spec.jar_root_ref
                juicer_tools_spec = None
                if jar_root_ref and jar_root_ref in resolver.packages:
                    juicer_tools_spec = resolver.packages[jar_root_ref]

                if effective_apply:
                    result = conform_juicer(package_spec, juicer_tools_spec)
                    conformance_results.append(result)
                    if result.status == "ok":
                        for action in result.actions_taken:
                            print(f"  ✓ {action}")
                    else:
                        print(f"  ❌ {result.detail}")
                else:
                    print(f"  [dry-run] Would conform {package_name} installation")
                    print("    - Create scripts -> CPU symlink if missing")
                    if juicer_tools_spec:
                        print("    - Link juicer_tools.jar into scripts/common/")

    if not update_requested and not effective_apply and any(
        pkg.conform_on_bootstrap for pkg in resolver.packages.values()
    ):
        print("\n💡 Run with --apply to execute conformance actions")

    # Step 4: Strictly verify and preflight all packages after conformance.
    post_conformance_issues = verify_envs_yaml(config_path)
    issues = [
        issue for issue in post_conformance_issues if issue not in initial_issues
    ]
    if issues:
        print("\n❌ Configuration issues found after conformance:")
        for issue in issues:
            print(f"  - {issue}")

    print("\nPackage preflight:")
    preflight_results = preflight_all(resolver)
    all_ok = not initial_issues and not issues and all(
        result.status == "ok" for result in conformance_results
    )
    for package_name, preflight_result in preflight_results:
        if preflight_result.status == "ok":
            print(f"  ✓ {package_name}: all canary paths present")
        else:
            print(f"  ❌ {package_name}: {preflight_result.detail}")
            if preflight_result.missing_paths:
                for path in preflight_result.missing_paths:
                    print(f"      - missing: {path}")
            print(
                "      Fix: check the package install_root/canary paths in "
                "config/envs.yaml, then rerun bootstrap."
            )
            all_ok = False

    print("\nMethod environment preflight:")
    try:
        method_results = preflight_methods(resolver)
    except (OSError, ValueError, RuntimeError) as exc:
        method_results = []
        print(f"  ❌ method preflight could not run: {exc}")
        print(
            "      Fix: correct the methods/default environment entries in "
            "config/envs.yaml, then rerun bootstrap."
        )
        all_ok = False
    if not method_results:
        print("  ✓ no configured methods")
    for method_result in method_results:
        required = ", ".join(method_result.required_tools) or "no required tools"
        if method_result.status == "ok":
            print(
                f"  ✓ {method_result.method_name} "
                f"[{method_result.env_target}]: tools available ({required})"
            )
            if method_result.package_tools:
                print(
                    "      package refs: "
                    + ", ".join(method_result.package_tools)
                )
            continue

        if method_result.status == "blocked_env":
            print(
                f"  ❌ {method_result.method_name} "
                f"[{method_result.env_target}]: environment probe failed"
            )
        else:
            missing = ", ".join(method_result.missing_tools)
            print(
                f"  ❌ {method_result.method_name} "
                f"[{method_result.env_target}]: missing tool(s): {missing}"
            )
        if method_result.detail:
            print(f"      Error: {method_result.detail}")
        print(
            "      Fix: install the listed executable(s) in the reported "
            f"conda target or correct its mapping (conda bin: "
            f"{method_result.conda_bin}), then rerun bootstrap."
        )
        all_ok = False

    # Step 5: Summary
    print("\n" + "=" * 60)
    if all_ok:
        print("✓ Bootstrap complete: configuration, packages, and methods validated")
        return 0
    else:
        print("❌ Bootstrap failed: fix the specific errors listed above")
        return 1


def main(argv: list[str] | None = None) -> int:
    """CLI main entry."""
    # Load .env once so all public commands share setdefault-style configuration.
    _load_dotenv_without_override()
    args = _parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "inspect":
        return _run_inspect(args)
    if args.command == "storage-audit":
        return _run_storage_audit(args)
    if args.command == "resume":
        return _run_resume(args)
    if args.command == "review":
        return _run_review(args)
    if args.command == "tui":
        return _run_tui(args)
    if args.command == "bootstrap":
        return _run_bootstrap(args)
    _parse_args(["--help"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
