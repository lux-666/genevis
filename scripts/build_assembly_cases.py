#!/usr/bin/env python3
"""Export explicitly selected v5 Assembly operations; never rewrite source graphs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge.v5_projection import project_paper


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extractions", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    selections = json.loads(args.selection.read_text())
    papers = sorted({s["paper"] for s in selections if s["stage"] == "assembly"})
    prepared = {}
    for paper_id in papers:
        if not paper_id.isalnum():
            raise ValueError("paper IDs must be simple alphanumeric names")
        graph = json.loads((args.extractions / f"{paper_id}.json").read_text())
        paper = project_paper(paper_id, graph, selections)
        payload = (paper.model_dump_json(indent=2) + "\n").encode()
        destination = args.output / f"{paper_id}.json"
        if destination.exists() and destination.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite different case: {destination}")
        prepared[destination] = payload
    args.output.mkdir(parents=True, exist_ok=True)
    for destination, payload in prepared.items():
        if not destination.exists():
            with destination.open("xb") as stream:
                stream.write(payload)
    print(f"Exported {len(prepared)} Assembly paper packages to {args.output}")


if __name__ == "__main__":
    main()
