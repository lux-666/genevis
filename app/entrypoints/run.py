"""Command-line entrypoint for gene-agent.

Usage:
    gene-agent run --session-id <id> --manifest <path> [--data-dir <dir>]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from app.control.ledger import Ledger
from app.schemas.entry import EntryRequest
from app.graph.builder import build_graph
from app.schemas.run_config import RunConfig, load_run_config
from app.schemas.state import initial_state


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
        choices=[
            "read_qc",
            "genome_survey",
            "assembly",
            "assembly_review",
            "hic_scaffolding",
            "repeat_annotation",
        ],
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
            "fields (local_exec stages and assembly.flye_parameters)."
        ),
    )

    ask_parser = subparsers.add_parser("ask", help="Start a workflow from a natural-language request")
    ask_parser.add_argument("request_text", help="Natural-language workflow-start request")
    ask_parser.add_argument("--session-id", help="Default session identifier if omitted from the request")
    ask_parser.add_argument("--data-dir", help="Default output data directory if omitted from the request")

    inspect_parser = subparsers.add_parser("inspect", help="Inspect a workflow session from durable records")
    inspect_parser.add_argument("--session-id", required=True, help="Session identifier to inspect")
    inspect_parser.add_argument("--data-dir", default="data", help="Output data directory")
    return parser.parse_args(argv)


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

    manifest_path = Path(request.manifest)
    if not manifest_path.exists():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    manifest_session_id = _peek_manifest_session_id(manifest_path)
    if request.session_id is None:
        adopted = manifest_session_id or generate_session_id(request.data_dir)
        try:
            request = EntryRequest(
                session_id=adopted,
                manifest=request.manifest,
                data_dir=request.data_dir,
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

    state = initial_state(
        session_id=request.session_id,
        input_manifest_ref=str(manifest_path),
        data_dir=request.data_dir,
    )

    stage_outputs = dict(state.get("stage_outputs") or {})
    if local_stages:
        for stage_name in local_stages:
            existing = dict(stage_outputs.get(stage_name) or {})
            existing["execution_mode"] = "local"
            stage_outputs[stage_name] = existing
    if run_config and run_config.assembly and run_config.assembly.flye_parameters:
        assembly_out = dict(stage_outputs.get("assembly") or {})
        assembly_out["flye_parameters"] = run_config.assembly.flye_parameters.model_dump()
        stage_outputs["assembly"] = assembly_out
    if stage_outputs != (state.get("stage_outputs") or {}):
        state["stage_outputs"] = stage_outputs

    graph = build_graph()
    result = graph.invoke(
        state,
        config={"configurable": {"thread_id": request.session_id}},
    )

    current_stage = result.get("current_stage", "unknown")
    route_trace = result.get("route_trace", [])
    supervision = result.get("supervision_state", "unknown")
    registry_ref = result.get("asset_registry_ref")
    stage_plan_ref = result.get("stage_plan_ref")
    active_interrupt = result.get("active_interrupt")

    print(f"session_id: {request.session_id}")
    print(f"current_stage: {current_stage}")
    print(f"supervision_state: {supervision}")
    print("checkpoint_persistence: memory_only")
    print("resume_note: durable cross-process resume is not available yet")
    print(f"route_trace: {route_trace}")
    if active_interrupt:
        print(f"active_interrupt: {active_interrupt}")
    if registry_ref:
        print(f"asset_registry_ref: {registry_ref}")
    if stage_plan_ref:
        print(f"stage_plan_ref: {stage_plan_ref}")

    if supervision == "failed_terminal":
        print(f"error: {result.get('last_error', 'unknown failure')}", file=sys.stderr)
        return 1

    return 0


def _run_ask(args: argparse.Namespace) -> int:
    _load_dotenv_without_override()
    from app.entrypoints.intent import parse_request_with_llm
    from app.entrypoints.manifest_materializer import materialize_entry_manifest

    data_dir = args.data_dir or "data"
    defaults = {
        "session_id": args.session_id or generate_session_id(data_dir),
        "data_dir": data_dir,
    }
    try:
        request = parse_request_with_llm(args.request_text, defaults=defaults)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        request = materialize_entry_manifest(request)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    structured_args = _parse_args(request.to_cli_argv())
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
    from app.control.lifecycle import inspect_session

    summary = inspect_session(args.session_id, args.data_dir)
    print(f"session_id: {summary['session_id']}")
    print(f"supervision_state: {summary['supervision_state']}")
    print(f"checkpoint_persistence: {summary['checkpoint_persistence']}")
    print(f"durable_resume_available: {str(summary['durable_resume_available']).lower()}")
    if summary.get("latest_review_request"):
        print(f"latest_review_request: {summary['latest_review_request']}")
    if summary.get("latest_review_request_artifact"):
        print(f"latest_review_request_artifact: {summary['latest_review_request_artifact']}")
    if summary.get("latest_review_decision"):
        print(f"latest_review_decision: {summary['latest_review_decision']}")
    if summary.get("error"):
        print(f"error: {summary['error']}", file=sys.stderr)
        return 1
    return 0


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


def main(argv: list[str] | None = None) -> int:
    """CLI main entry."""
    args = _parse_args(argv)
    if args.command == "run":
        return _run_session(args)
    if args.command == "ask":
        return _run_ask(args)
    if args.command == "inspect":
        return _run_inspect(args)
    _parse_args(["--help"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
