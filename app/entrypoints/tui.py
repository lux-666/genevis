"""Interactive TUI for gene-agent.

Entry point: `gene-agent tui` (optional `--data-dir` only).

Flow:
  1. User types a natural-language request (same as `gene-agent ask`)
  2. LLM parses intent, materializes manifest
  3. Interactive execution-route plan (stages, methods, known static inputs)
  4. Runtime-derived proposals appear only when prerequisite evidence exists
  5. The same conversation handles every reached human gate and resumes execution

Operator entry surfaces (do not mix):
  - `gene-agent tui` — conversational plan/run/gates. Start the TUI; use an
    in-app command such as `/resume` to open a session picker and continue a
    blocked durable session. Do not stitch `tui --resume-session <id>` as the
    product resume path (that flag is a temporary CLI hook, not TUI UX).
  - `gene-agent review --session-id ...` — interactive Decision Brief for one
    known blocked session; not the full TUI chat.
  - `gene-agent resume --session-id ... --decision-file ...` — non-interactive
    bound resume after `inspect --write-decision-template` (smoke/scripts).
"""

from __future__ import annotations

import argparse
import sys


def run_tui(data_dir: str, *, resume_session: str | None = None) -> int:
    """Launch a conversational workflow or reconnect to one durable human gate."""
    try:
        if resume_session:
            from app.entrypoints.review_interactive import run_interactive_review

            return run_interactive_review(resume_session, data_dir)
        return _tui_main(data_dir)
    except (KeyboardInterrupt, EOFError):
        print("\nAborted during interactive planning.")
        return 1


def _tui_main(data_dir: str) -> int:
    from app.entrypoints.ask_interactive import run_interactive_loop
    from app.entrypoints.run import _create_ask_plan

    print("\n=== gene-agent interactive ===\n")
    print(
        "Describe what you want to do (e.g. "
        "'对 /path/reads.fa 以卵鳎为目标物种进行 T2T 级别基因组组装'):"
    )

    try:
        request_text = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted during request entry.")
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
