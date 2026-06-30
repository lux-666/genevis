"""Interactive TUI for gene-agent.

Entry point: `gene-agent tui`

Flow:
  1. User types a natural-language request (same as `gene-agent ask`)
  2. LLM parses intent, materializes manifest
  3. Interactive plan selection (stages, methods, parameters)
  4. Execution with automatic human-gate handling (no exit on blocked_human)
"""

from __future__ import annotations

import argparse
import sys


def run_tui(data_dir: str) -> int:
    """Top-level TUI entry. Returns CLI exit code."""
    try:
        return _tui_main(data_dir)
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 1


def _tui_main(data_dir: str) -> int:
    from app.entrypoints.ask_interactive import run_interactive_loop
    from app.entrypoints.run import _create_ask_plan

    print("\n=== gene-agent interactive ===\n")
    print("Describe what you want to do (e.g. '对 /path/reads.fa 以卵鳎为目标物种进行 T2T 级别基因组组装'):")

    try:
        request_text = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return 1

    if not request_text:
        print("error: empty request", file=sys.stderr)
        return 1

    args = argparse.Namespace(
        request_text=request_text,
        data_dir=data_dir,
        session_id=None,
        json=False,
        non_interactive=False,
        resume_session=None,
        set_params=[],
        checkpointer="sqlite",
    )

    print("\nParsing intent...\n")
    try:
        plan = _create_ask_plan(args)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Enable interactive gate handling during execution
    args._interactive_gate = True

    return run_interactive_loop(plan, args)
