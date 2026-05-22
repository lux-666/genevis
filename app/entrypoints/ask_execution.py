"""Execute a confirmed ask plan as a structured run invocation."""

from __future__ import annotations

import argparse
import sys

from app.entrypoints.ask_planner import write_ask_run_config
from app.schemas.ask_plan import AskPlanState
from app.schemas.entry import EntryRequest


def execute_confirmed_plan(plan: AskPlanState, args: argparse.Namespace) -> int:
    from app.entrypoints.run import _parse_args, _run_session

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
