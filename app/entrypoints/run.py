"""Command-line entrypoint for gene-agent.

Usage:
    gene-agent run --session-id <id> --manifest <path> [--data-dir <dir>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.graph.builder import build_graph
from app.schemas.state import initial_state


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gene-agent")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a genome workflow session")
    run_parser.add_argument("--session-id", required=True, help="Unique session identifier")
    run_parser.add_argument("--manifest", required=True, help="Path to input manifest YAML/JSON")
    run_parser.add_argument("--data-dir", default="data", help="Output data directory")
    return parser.parse_args(argv)


def _run_session(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    state = initial_state(
        session_id=args.session_id,
        input_manifest_ref=str(manifest_path),
        data_dir=args.data_dir,
    )

    graph = build_graph()
    result = graph.invoke(
        state,
        config={"configurable": {"thread_id": args.session_id}},
    )

    current_stage = result.get("current_stage", "unknown")
    route_trace = result.get("route_trace", [])
    registry_ref = result.get("asset_registry_ref")

    print(f"session: {args.session_id}")
    print(f"current_stage: {current_stage}")
    print(f"route_trace: {route_trace}")
    if registry_ref:
        print(f"asset_registry: {registry_ref}")

    supervision = result.get("supervision_state", "unknown")
    if supervision == "failed_terminal":
        print(f"error: {result.get('last_error', 'unknown failure')}", file=sys.stderr)
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI main entry."""
    args = _parse_args(argv)
    if args.command == "run":
        return _run_session(args)
    else:
        _parse_args(["--help"])
        return 0


if __name__ == "__main__":
    sys.exit(main())
