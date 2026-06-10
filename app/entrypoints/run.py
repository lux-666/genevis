"""Command-line entrypoint for gene-agent.

Usage:
    gene-agent run --session-id <id> --manifest <path> [--data-dir <dir>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from app.control.ledger import Ledger
from app.schemas.entry import EntryRequest
from app.graph.builder import build_graph
from app.graph.checkpointer import DEFAULT_CHECKPOINTER_BACKEND
from app.schemas.run_config import LocalExecStage, RunConfig, load_run_config
from app.schemas.state import initial_state


_LOCAL_EXEC_STAGE_CHOICES = list(get_args(LocalExecStage))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gene-agent")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a genome workflow session")
    run_parser.add_argument("--session-id", help="Unique session identifier; generated when omitted")
    run_parser.add_argument("--manifest", required=True, help="Path to input manifest YAML/JSON")
    run_parser.add_argument("--data-dir", default="data", help="Output data directory")
    run_parser.add_argument(
        "--local-exec",
        action="append",
        default=[],
        metavar="STAGE",
        choices=_LOCAL_EXEC_STAGE_CHOICES,
        help=(
            "Opt a stage into local execution by seeding "
            "stage_outputs[STAGE].execution_mode=local. Repeat the flag to "
            "enable multiple stages."
        ),
    )
    run_parser.add_argument(
        "--run-config",
        dest="run_config",
        default=None,
        help=(
            "Path to a JSON run-config file that seeds approved stage_outputs "
            "fields (local_exec stages, assembly.flye_parameters, and "
            "gene/function annotation execution hints)."
        ),
    )
    run_parser.add_argument(
        "--checkpointer",
        choices=["sqlite", "memory"],
        default=None,
        help=(
            "LangGraph checkpoint backend. 'sqlite' (default) persists "
            "checkpoints under <data_dir>/checkpoints/ so a blocked "
            "session can be resumed across processes; 'memory' is "
            "in-process only and intended for tests. Falls back to the "
            "GENE_AGENT_CHECKPOINTER env var when omitted."
        ),
    )

    ask_parser = subparsers.add_parser("ask", help="Start a workflow from a natural-language request")
    ask_parser.add_argument("request_text", nargs="?", default=None, help="Natural-language request; required unless --ask-plan is provided")
    ask_parser.add_argument("--session-id", help="Default session identifier if omitted from the request")
    ask_parser.add_argument("--data-dir", help="Default output data directory if omitted from the request")
    ask_parser.add_argument("--ask-plan", dest="ask_plan", default=None, help="Continue a previously emitted ask clarification plan")
    ask_parser.add_argument("--clarification-response", dest="clarification_response", default=None, help="JSON response to the current clarification request")
    ask_parser.add_argument("--write-clarification-template", dest="write_clarification_template", default=None, help="Write a clarification response template to a file")
    ask_parser.add_argument("--json", action="store_true", default=False, help="Emit clarification payloads as JSON")
    ask_parser.add_argument(
        "--run-config",
        dest="run_config",
        default=None,
        help="Path to a JSON run-config file; bypasses clarification planning.",
    )
    ask_parser.add_argument(
        "--local-exec",
        action="append",
        default=[],
        metavar="STAGE",
        choices=_LOCAL_EXEC_STAGE_CHOICES,
        help="Opt a stage into local execution (used with --run-config bypass).",
    )
    ask_parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=False,
        help="Exit after first clarification (for automation/scripts).",
    )
    ask_parser.add_argument(
        "--resume",
        dest="resume_session",
        default=None,
        metavar="SESSION_ID",
        help="Resume an existing ask session by session ID.",
    )
    ask_parser.add_argument(
        "--set",
        dest="set_params",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pre-supply parameter values (e.g. --set genome_size=500m).",
    )
    ask_parser.add_argument(
        "--checkpointer",
        choices=["sqlite", "memory"],
        default=None,
        help="Checkpoint backend for the structured run launched by ask.",
    )

    inspect_parser = subparsers.add_parser("inspect", help="Inspect a workflow session from durable records")
    inspect_parser.add_argument("--session-id", required=True, help="Session identifier to inspect")
    inspect_parser.add_argument("--data-dir", default="data", help="Output data directory")
    inspect_parser.add_argument("--filter", action="append", default=[], help="Filter inspect output, currently supports decided_by=<origin>")
    inspect_parser.add_argument("--json", action="store_true", help="Emit a machine-readable inspect summary")
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

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume a blocked workflow session from a structured decision file",
    )
    resume_parser.add_argument("--session-id", required=True, help="Session identifier to resume")
    resume_parser.add_argument(
        "--decision-file",
        dest="decision_file",
        required=True,
        help="Path to a JSON decision file matching ResumeReviewRequest",
    )
    resume_parser.add_argument("--data-dir", default="data", help="Output data directory")
    resume_parser.add_argument(
        "--max-blocked-seconds",
        type=float,
        default=None,
        help="Reject/convert a blocked resume if the gate has been blocked for at least this many seconds",
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
    review_parser.add_argument("--session-id", required=True, help="Session identifier to review")
    review_parser.add_argument("--data-dir", default="data", help="Output data directory")
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
        help="Interactive workflow launcher with automatic human-gate handling",
    )
    tui_parser.add_argument("--data-dir", default="data", help="Output data directory")
    return parser.parse_args(argv)


def _resolve_checkpointer_backend(args: argparse.Namespace) -> str:
    """Resolve the checkpoint backend with explicit > env var > default.

    Lets `gene-agent run` and `gene-agent resume` default to durable
    sqlite for operators while letting the test suite (and any caller
    that exports ``GENE_AGENT_CHECKPOINTER=memory``) opt out without
    threading kwargs through every call site.
    """
    explicit = getattr(args, "checkpointer", None)
    if explicit:
        return explicit
    return os.environ.get("GENE_AGENT_CHECKPOINTER") or DEFAULT_CHECKPOINTER_BACKEND


def _resolve_data_dir(data_dir: str) -> str:
    """Resolve CLI data_dir once so generated stage scripts get stable roots."""
    return str(Path(data_dir).expanduser().resolve(strict=False))


def _peek_manifest_session_id(manifest_path: Path) -> str | None:
    """Return the manifest's top-level session_id field, or None.

    Used for an early-fail cross-check against the CLI --session-id. Full
    manifest validation still runs later in the inventory stage; this peek
    must not throw on malformed input.
    """
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return None
    suffix = manifest_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        import yaml

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
    elif suffix == ".json":
        import json

        try:
            data = json.loads(text)
        except ValueError:
            return None
    else:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("session_id")
    if isinstance(value, str) and value:
        return value
    return None


def _run_session(args: argparse.Namespace) -> int:
    try:
        request = EntryRequest(session_id=args.session_id, manifest=args.manifest, data_dir=args.data_dir)
    except ValueError as exc:
        print(f"error: invalid run request: {exc}", file=sys.stderr)
        return 1

    backend = _resolve_checkpointer_backend(args)
    data_dir = _resolve_data_dir(request.data_dir)

    manifest_path = Path(request.manifest)
    if not manifest_path.exists():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    manifest_session_id = _peek_manifest_session_id(manifest_path)
    if request.session_id is None:
        adopted = manifest_session_id or generate_session_id(data_dir)
        try:
            request = EntryRequest(
                session_id=adopted,
                manifest=request.manifest,
                data_dir=data_dir,
            )
        except ValueError as exc:
            print(f"error: invalid run request: {exc}", file=sys.stderr)
            return 1
    elif manifest_session_id is not None and manifest_session_id != request.session_id:
        print(
            "error: manifest session_id "
            f"{manifest_session_id!r} does not match CLI --session-id "
            f"{request.session_id!r}; the asset registry would land under "
            "the manifest id while every other stage would write under the "
            "CLI id. Make them match (drop one, or align the values).",
            file=sys.stderr,
        )
        return 1
    else:
        try:
            request = EntryRequest(
                session_id=request.session_id,
                manifest=request.manifest,
                data_dir=data_dir,
            )
        except ValueError as exc:
            print(f"error: invalid run request: {exc}", file=sys.stderr)
            return 1

    run_config: RunConfig | None = None
    run_config_path = getattr(args, "run_config", None)
    if run_config_path:
        try:
            run_config = load_run_config(run_config_path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    cli_local_stages = list(getattr(args, "local_exec", []) or [])
    config_local_stages = list(run_config.local_exec) if run_config else []
    local_stages = list(dict.fromkeys([*cli_local_stages, *config_local_stages]))

    if "assembly" in local_stages and not (
        run_config
        and run_config.assembly
        and run_config.assembly.flye_parameters
    ):
        print(
            "error: local execution of 'assembly' requires "
            "run_config.assembly.flye_parameters; pass --run-config with "
            "flye_parameters or remove 'assembly' from --local-exec",
            file=sys.stderr,
        )
        return 1

    Ledger(request.data_dir).create_session(request.session_id, str(manifest_path))

    requested_stages = list(run_config.requested_stages) if run_config and run_config.requested_stages else None
    state = initial_state(
        session_id=request.session_id,
        input_manifest_ref=str(manifest_path),
        data_dir=request.data_dir,
        requested_stages=requested_stages,
    )

    stage_outputs = dict(state.get("stage_outputs") or {})
    if local_stages:
        for stage_name in local_stages:
            existing = dict(stage_outputs.get(stage_name) or {})
            existing["execution_mode"] = "local"
            stage_outputs[stage_name] = existing
    if run_config and run_config.shared_parameters:
        state["shared_parameters"] = run_config.shared_parameters.model_dump(exclude_none=True)
    if run_config and run_config.read_qc and run_config.read_qc.length_filter:
        read_qc_out = dict(stage_outputs.get("read_qc") or {})
        read_qc_out["length_filter_request"] = run_config.read_qc.length_filter.model_dump(mode="json")
        stage_outputs["read_qc"] = read_qc_out
    if run_config and run_config.genome_survey and run_config.genome_survey.survey_parameters:
        survey_out = dict(stage_outputs.get("genome_survey") or {})
        survey_out["survey_parameters"] = run_config.genome_survey.survey_parameters.model_dump(exclude_none=True)
        stage_outputs["genome_survey"] = survey_out
    if "repeat_annotation" in local_stages:
        repeat_out = dict(stage_outputs.get("repeat_annotation") or {})
        repeat_out.setdefault(
            "primary_method", "repeat_annotation.masking.repeatmasker"
        )
        stage_outputs["repeat_annotation"] = repeat_out
    if run_config and run_config.assembly and run_config.assembly.flye_parameters:
        assembly_out = dict(stage_outputs.get("assembly") or {})
        assembly_out["flye_parameters"] = run_config.assembly.flye_parameters.model_dump(exclude_none=True)
        stage_outputs["assembly"] = assembly_out
    if run_config and run_config.assembly_review:
        review_out = dict(stage_outputs.get("assembly_review") or {})
        if run_config.assembly_review.quast:
            review_out["quast_parameters"] = run_config.assembly_review.quast.model_dump()
        if run_config.assembly_review.busco:
            review_out["busco_parameters"] = run_config.assembly_review.busco.model_dump()
        review_out["max_reruns"] = run_config.assembly_review.max_reruns
        stage_outputs["assembly_review"] = review_out
    if run_config and run_config.review_policy:
        for review_stage in ["assembly_review", "hic_scaffolding"]:
            stage_out = dict(stage_outputs.get(review_stage) or {})
            stage_out["review_policy"] = run_config.review_policy.effective_for_stage(review_stage)
            stage_outputs[review_stage] = stage_out
    if run_config and run_config.gene_annotation and run_config.gene_annotation.lineage_or_species_hints:
        gene_out = dict(stage_outputs.get("gene_annotation") or {})
        gene_out["lineage_or_species_hints"] = dict(run_config.gene_annotation.lineage_or_species_hints)
        stage_outputs["gene_annotation"] = gene_out
    if run_config and run_config.function_annotation and run_config.function_annotation.database_provenance:
        fn_out = dict(stage_outputs.get("function_annotation") or {})
        fn_out["database_provenance"] = [
            p.model_dump(mode="json") for p in run_config.function_annotation.database_provenance
        ]
        stage_outputs["function_annotation"] = fn_out
    if stage_outputs != (state.get("stage_outputs") or {}):
        state["stage_outputs"] = stage_outputs

    graph = build_graph(backend=backend, data_dir=request.data_dir)
    config = {"configurable": {"thread_id": request.session_id}}
    result = graph.invoke(state, config=config)

    interactive_gate = getattr(args, "_interactive_gate", None)
    if interactive_gate is None:
        interactive_gate = sys.stdin.isatty()
    supervision = result.get("supervision_state", "unknown")

    if supervision == "blocked_human" and interactive_gate and backend == "sqlite":
        result = _interactive_gate_loop(graph, config, result, request.session_id, request.data_dir)
        supervision = result.get("supervision_state", "unknown")

    current_stage = result.get("current_stage", "unknown")
    route_trace = result.get("route_trace", [])
    registry_ref = result.get("asset_registry_ref")
    stage_plan_ref = result.get("stage_plan_ref")
    active_interrupt = result.get("active_interrupt")

    print(f"session_id: {request.session_id}")
    print(f"current_stage: {current_stage}")
    print(f"supervision_state: {supervision}")
    print(f"checkpoint_persistence: {backend}")
    if supervision == "blocked_human":
        if backend == "sqlite":
            print(
                "resume_note: use `gene-agent review` or `gene-agent resume` to continue"
            )
        else:
            print("resume_note: durable cross-process resume requires --checkpointer sqlite")
    print(f"route_trace: {route_trace}")
    if active_interrupt:
        print(f"active_interrupt: {active_interrupt}")
    if registry_ref:
        print(f"asset_registry_ref: {registry_ref}")
    if stage_plan_ref:
        print(f"stage_plan_ref: {stage_plan_ref}")
    if run_config_path:
        run_config_ref = Path(run_config_path).expanduser().resolve(strict=False)
        print(f"run_config_ref: {run_config_ref}")

    if supervision == "failed_terminal":
        print(f"error: {result.get('last_error', 'unknown failure')}", file=sys.stderr)
        return 1

    return 0


def _interactive_gate_loop(
    graph: Any,
    config: dict,
    result: dict,
    session_id: str,
    data_dir: str,
) -> dict:
    """Loop on human gates: prompt operator, resume, repeat until not blocked."""
    from app.control.lifecycle import (
        DurableResumeUnavailableError,
        ResumeValidationError,
        resume_review_state,
    )
    from app.entrypoints.review_interactive import collect_gate_decision, persist_decision_artifact
    from app.review.annotation_intake import collect_annotation_intake_selection, persist_annotation_intake_plan

    ledger = Ledger(data_dir)

    while result.get("supervision_state") == "blocked_human":
        gate_result = collect_gate_decision(session_id, data_dir)
        if gate_result is None:
            print("review skipped; session remains blocked")
            break

        request, raw_decision = gate_result

        snapshot = graph.get_state(config)
        if not snapshot or not snapshot.values:
            print("error: checkpoint lost", file=sys.stderr)
            break

        state = snapshot.values

        intake_selection = None
        try:
            intake_selection = collect_annotation_intake_selection(
                state=state,
                request=request,
            )
            update = resume_review_state(state, request)
            intake = None
            if intake_selection is not None:
                merged_state = {**state, **update}
                intake = persist_annotation_intake_plan(
                    state=merged_state,
                    recommended_plan=intake_selection.recommended_plan,
                    final_plan=intake_selection.final_plan,
                    request=request,
                    source="interactive_gate",
                )
                update = {**update, **intake.update}
        except (ResumeValidationError, DurableResumeUnavailableError) as exc:
            ledger.record_event(session_id, "resume_rejected", {
                "source": "interactive_gate",
                "error": str(exc),
                "decision": {"stage": request.stage, "decision": request.decision},
            })
            print(f"error: resume rejected: {exc}", file=sys.stderr)
            break
        except RuntimeError as exc:
            ledger.record_event(session_id, "resume_rejected", {
                "source": "interactive_gate",
                "error": str(exc),
                "decision": {"stage": request.stage, "decision": request.decision},
            })
            print(f"error: resume rejected: {exc}", file=sys.stderr)
            break

        decision_path = persist_decision_artifact(raw_decision, request.stage, session_id, data_dir)
        print(f"decision_artifact: {decision_path}")
        if intake is not None:
            print(f"annotation_intake_plan: {intake.artifact_ref}")

        resume_payload = {
            "source": "interactive_gate",
            "decision_artifact_ref": str(decision_path),
            "decision": {"stage": request.stage, "gate_id": request.gate_id, "decision": request.decision},
        }
        ledger.record_event(session_id, "resume_started", resume_payload)

        graph.update_state(config, update)
        try:
            result = graph.invoke(None, config=config)
        except Exception as exc:
            ledger.record_event(session_id, "resume_failed", {**resume_payload, "error": str(exc)})
            print(f"error: resume invoke failed: {exc}", file=sys.stderr)
            break

        ledger.record_event(session_id, "resume_completed", {
            **resume_payload,
            "current_stage": result.get("current_stage"),
            "supervision_state": result.get("supervision_state"),
        })
        print(f"\nresumed → stage={result.get('current_stage')} state={result.get('supervision_state')}")

    return result


def _run_ask(args: argparse.Namespace) -> int:
    from app.entrypoints.ask_interactive import apply_set_params, is_interactive, run_interactive_loop
    from app.entrypoints.ask_planner import (
        ask_plan_path,
        clarification_template,
        merge_clarification_response,
        next_clarification,
        save_plan_to_default_path,
        write_ask_run_config,
    )
    from app.schemas.ask_plan import AskPlanState, load_ask_clarification_response, load_ask_plan

    if getattr(args, "run_config", None):
        return _run_ask_with_explicit_config(args)

    if getattr(args, "local_exec", None):
        print(
            "error: --local-exec is only valid with --run-config in ask mode; "
            "the clarification flow manages execution stages automatically",
            file=sys.stderr,
        )
        return 1

    try:
        if getattr(args, "resume_session", None):
            data_dir = args.data_dir or "data"
            ref = ask_plan_path(data_dir, args.resume_session)
            plan = load_ask_plan(ref)
        elif getattr(args, "ask_plan", None):
            plan = load_ask_plan(args.ask_plan)
        else:
            if not getattr(args, "request_text", None):
                print("error: request_text is required unless --ask-plan or --resume is provided", file=sys.stderr)
                return 1
            plan = _create_ask_plan(args)

        if getattr(args, "clarification_response", None):
            response = load_ask_clarification_response(args.clarification_response)
            plan = merge_clarification_response(plan, response)

        if getattr(args, "set_params", None):
            plan = apply_set_params(plan, args.set_params)

        plan_ref = save_plan_to_default_path(plan)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if is_interactive(args):
        return run_interactive_loop(plan, args)

    try:
        clarification = next_clarification(plan, ask_plan_ref=plan_ref)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if clarification:
        if getattr(args, "write_clarification_template", None):
            template = clarification_template(clarification)
            template_path = Path(args.write_clarification_template).expanduser().resolve(strict=False)
            template_path.parent.mkdir(parents=True, exist_ok=True)
            template_path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        if getattr(args, "json", False):
            print(json.dumps(clarification.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            print(f"status: {clarification.status}")
            print(f"clarification.kind: {clarification.kind}")
            print(f"session_id: {clarification.session_id}")
            print(f"ask_plan_ref: {clarification.ask_plan_ref}")
            print(f"manifest_ref: {clarification.manifest_ref}")
            print(f"question: {clarification.question}")
            if clarification.summary:
                print(f"summary: {json.dumps(clarification.summary, ensure_ascii=False, indent=2, default=str)}")
            print(f"required_fields: {clarification.required_fields}")
            print(f"next_action.command: {clarification.next_action.get('command')}")
        return 0

    try:
        run_config_path = str(write_ask_run_config(plan))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    request = EntryRequest.model_validate(plan.entry_request)
    if not request.manifest:
        request = request.model_copy(update={"manifest": plan.manifest_ref})
    argv = request.to_cli_argv()
    argv.extend(["--run-config", run_config_path])
    if getattr(args, "checkpointer", None):
        argv.extend(["--checkpointer", args.checkpointer])
    structured_args = _parse_args(argv)
    return _run_session(structured_args)


def _create_ask_plan(args: argparse.Namespace) -> "AskPlanState":
    from app.entrypoints.intent import parse_request_with_llm
    from app.entrypoints.manifest_materializer import materialize_entry_manifest
    from app.schemas.ask_plan import AskPlanState

    data_dir = args.data_dir or "data"
    defaults = {
        "session_id": args.session_id or generate_session_id(data_dir),
        "data_dir": data_dir,
    }
    request = parse_request_with_llm(args.request_text, defaults=defaults)
    request = materialize_entry_manifest(request)
    stored_request = request.model_copy(update={"source_paths": []}) if request.manifest else request
    return AskPlanState(
        session_id=str(request.session_id),
        data_dir=request.data_dir,
        request_text=args.request_text,
        entry_request=stored_request.model_dump(mode="json"),
        manifest_ref=str(request.manifest),
    )


def _run_ask_with_explicit_config(args: argparse.Namespace) -> int:
    from app.entrypoints.intent import parse_request_with_llm
    from app.entrypoints.manifest_materializer import materialize_entry_manifest

    if not getattr(args, "request_text", None):
        print("error: request_text is required for gene-agent ask --run-config", file=sys.stderr)
        return 1
    data_dir = args.data_dir or "data"
    defaults = {
        "session_id": args.session_id or generate_session_id(data_dir),
        "data_dir": data_dir,
    }
    try:
        request = parse_request_with_llm(args.request_text, defaults=defaults)
        request = materialize_entry_manifest(request)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    argv = request.to_cli_argv()
    argv.extend(["--run-config", args.run_config])
    for stage in getattr(args, "local_exec", []) or []:
        argv.extend(["--local-exec", stage])
    if getattr(args, "checkpointer", None):
        argv.extend(["--checkpointer", args.checkpointer])
    structured_args = _parse_args(argv)
    return _run_session(structured_args)


def generate_session_id(data_dir: str | Path = "data", *, now: datetime | None = None) -> str:
    date = (now or datetime.now()).strftime("%Y%m%d")
    ledger_root = Path(data_dir) / "ledger"
    pattern = re.compile(rf"^gene-{date}-(\d{{6}})$")
    max_index = 0
    if ledger_root.exists():
        for child in ledger_root.iterdir():
            if not child.is_dir():
                continue
            match = pattern.match(child.name)
            if match:
                max_index = max(max_index, int(match.group(1)))
    return f"gene-{date}-{max_index + 1:06d}"


def _run_inspect(args: argparse.Namespace) -> int:
    from app.control.lifecycle import ResumeValidationError, build_resume_decision_template, inspect_session

    filters = _parse_inspect_filters(getattr(args, "filter", []) or [])
    if filters is None:
        return 1
    data_dir = _resolve_data_dir(args.data_dir)
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
        out_path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        resume_command = (
            f"gene-agent resume --session-id {args.session_id} --data-dir {data_dir} "
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


def _print_inspect_text(summary: dict, filters: dict[str, str]) -> None:
    print(f"session_id: {summary['session_id']}")
    print(f"supervision_state: {summary['supervision_state']}")
    print(f"checkpoint_persistence: {summary['checkpoint_persistence']}")
    print(f"durable_resume_available: {str(summary['durable_resume_available']).lower()}")
    print(f"artifact_count: {summary.get('artifact_count', 0)}")
    print(f"decisions_by_origin: {summary.get('decisions_by_origin', {})}")
    if summary.get("latest_review_request"):
        print(f"latest_review_request: {summary['latest_review_request']}")
    if summary.get("latest_review_request_artifact"):
        print(f"latest_review_request_artifact: {summary['latest_review_request_artifact']}")
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
            print(f"decision_requirements.candidate_ids: {requirements['candidate_ids']}")
        if requirements.get("candidate_methods"):
            print(f"decision_requirements.candidate_methods: {requirements['candidate_methods']}")
    if summary.get("latest_review_decision"):
        print(f"latest_review_decision: {summary['latest_review_decision']}")
    review_decisions = _filter_review_decisions(summary.get("review_decisions") or [], filters)
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
            print(f"error: unsupported inspect filter {raw_filter!r}; expected key=value", file=sys.stderr)
            return None
        key, value = raw_filter.split("=", 1)
        if key != "decided_by":
            print(f"error: unsupported inspect filter key {key!r}; only decided_by is supported", file=sys.stderr)
            return None
        parsed[key] = value
    return parsed


def _filter_review_decisions(decisions: list[dict], filters: dict[str, str]) -> list[dict]:
    decided_by = filters.get("decided_by")
    if not decided_by:
        return decisions
    return [decision for decision in decisions if decision.get("decided_by") == decided_by]


def _load_dotenv_without_override() -> None:
    for path in _candidate_dotenv_paths():
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


def _run_resume(args: argparse.Namespace) -> int:
    from app.control.lifecycle import (
        DurableResumeUnavailableError,
        ResumeValidationError,
        UnsupportedLifecycleOperationError,
        resume_review_state,
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

    # The downstream `resume_review_state` re-checks that
    # state.session_id == decision.session_id. That is intentional
    # defense-in-depth: the helper is also called directly by the
    # in-process resume tests in `tests/test_review_resume.py`, where
    # this CLI guard does not run. Keep both checks in place.

    from app.graph.checkpointer import checkpoint_db_path

    db_path = checkpoint_db_path(args.data_dir)
    if not db_path.exists():
        print(
            f"error: no durable checkpoint database at {db_path}; "
            "check that --data-dir matches the original gene-agent run, "
            "or run the workflow with --checkpointer sqlite before resuming",
            file=sys.stderr,
        )
        return 1

    graph = build_graph(backend=backend, data_dir=args.data_dir)
    config = {"configurable": {"thread_id": args.session_id}}
    snapshot = graph.get_state(config)
    # New LangGraph versions can return a default snapshot rather than
    # None when the thread is missing, so cross-check session_id too —
    # this is the only reliable signal that the checkpoint actually
    # belongs to the session being resumed.
    if (
        snapshot is None
        or not snapshot.values
        or snapshot.values.get("session_id") != args.session_id
    ):
        print(
            f"error: no checkpoint found for session_id {args.session_id!r} "
            f"in {db_path}",
            file=sys.stderr,
        )
        return 1

    state = snapshot.values

    ledger = Ledger(args.data_dir)
    resume_event_payload = {
        "source": "cli",
        "checkpointer": backend,
        "data_dir": str(args.data_dir),
        "decision_file_ref": str(args.decision_file),
        "max_blocked_seconds": getattr(args, "max_blocked_seconds", None),
        "decision": {
            "stage": decision.stage,
            "gate_id": decision.gate_id,
            "decision": decision.decision,
            "candidate_id": decision.candidate_id,
            "method_name": decision.method_name,
            "decided_by": decision.decided_by,
            "rationale_artifact_ref": decision.rationale_artifact_ref,
        },
    }
    # Record resume_started before any state mutation so the audit
    # trail captures the operator's intent even if validation, the
    # state update, or the downstream invoke fails.
    ledger.record_event(args.session_id, "resume_started", resume_event_payload)

    try:
        update = resume_review_state(state, decision, blocked_timeout_seconds=getattr(args, "max_blocked_seconds", None))
    except (ResumeValidationError, DurableResumeUnavailableError) as exc:
        ledger.record_event(
            args.session_id,
            "resume_failed",
            {**resume_event_payload, "error": f"{type(exc).__name__}: {exc}"},
        )
        print(f"error: resume rejected: {exc}", file=sys.stderr)
        return 1
    except UnsupportedLifecycleOperationError as exc:
        ledger.record_event(
            args.session_id,
            "resume_failed",
            {**resume_event_payload, "error": f"{type(exc).__name__}: {exc}"},
        )
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Continue-downstream at assembly_review hands off to the annotation agent
    # via annotation.intake_plan. A non-interactive decision-file resume must
    # still record the recommended intake plan so gene_annotation can enter.
    from app.review.annotation_intake import maybe_collect_annotation_intake

    intake = maybe_collect_annotation_intake(
        state=state,
        resume_update=update,
        request=decision,
        source="cli",
        interactive=False,
    )
    if intake is not None:
        update = {**update, **intake.update}
        print(f"annotation_intake_plan: {intake.artifact_ref}")

    graph.update_state(config, update)
    try:
        result = graph.invoke(None, config=config)
    except Exception as exc:
        ledger.record_event(
            args.session_id,
            "resume_failed",
            {**resume_event_payload, "error": f"{type(exc).__name__}: {exc}"},
        )
        # Match the other subcommands: surface the failure as a CLI
        # error line and a non-zero exit code rather than letting a
        # traceback leak to the operator's terminal. The underlying
        # exception is still recorded in the ledger.
        print(f"error: resume invoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    current_stage = result.get("current_stage", "unknown")
    supervision = result.get("supervision_state", "unknown")
    route_trace = result.get("route_trace", [])
    active_interrupt = result.get("active_interrupt")

    ledger.record_event(
        args.session_id,
        "resume_completed",
        {
            **resume_event_payload,
            "current_stage": current_stage,
            "supervision_state": supervision,
            "route_trace": route_trace,
        },
    )

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
    data_dir = _resolve_data_dir(args.data_dir)
    return run_interactive_review(
        args.session_id,
        data_dir,
        dry_run=getattr(args, "dry_run", False),
    )


def _run_tui(args: argparse.Namespace) -> int:
    from app.entrypoints.tui import run_tui

    data_dir = _resolve_data_dir(args.data_dir)
    return run_tui(data_dir)


def main(argv: list[str] | None = None) -> int:
    """CLI main entry."""
    args = _parse_args(argv)
    # Load .env once at the entry point so every subcommand (run, ask,
    # inspect, resume) sees the same secrets without re-implementing the
    # loader in each branch. setdefault semantics mean already-exported
    # environment variables still win.
    _load_dotenv_without_override()
    if args.command == "run":
        return _run_session(args)
    if args.command == "ask":
        return _run_ask(args)
    if args.command == "inspect":
        return _run_inspect(args)
    if args.command == "resume":
        return _run_resume(args)
    if args.command == "review":
        return _run_review(args)
    if args.command == "tui":
        return _run_tui(args)
    _parse_args(["--help"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
