"""Execute a confirmed ask plan as a structured run invocation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.entrypoints.ask_planner import write_ask_run_config
from app.entrypoints.manifest_materializer import materialize_manifest_genome_size
from app.schemas.ask_plan import AskPlanState
from app.schemas.entry import EntryRequest


def execute_confirmed_plan(plan: AskPlanState, args: argparse.Namespace) -> int:
    from app.entrypoints.run import _parse_args, _run_session

    try:
        genome_size = (
            plan.shared_parameters.estimated_genome_size
            if plan.shared_parameters is not None
            else None
        )
        manifest_path = materialize_manifest_genome_size(
            manifest_path=Path(plan.manifest_ref),
            data_dir=plan.data_dir,
            session_id=plan.session_id,
            genome_size_estimate=genome_size,
        )
        run_config_path = str(write_ask_run_config(plan))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    entry_request = dict(plan.entry_request)
    entry_request.pop("intent_parse_source", None)
    request = EntryRequest.model_validate(entry_request)
    request = request.model_copy(update={"manifest": str(manifest_path)})
    argv = request.to_cli_argv()
    argv.extend(["--run-config", run_config_path])
    if getattr(args, "checkpointer", None):
        argv.extend(["--checkpointer", args.checkpointer])
    structured_args = _parse_args(argv)
    if getattr(args, "_interactive_gate", None) is not None:
        structured_args._interactive_gate = args._interactive_gate
    return _run_session(structured_args)
