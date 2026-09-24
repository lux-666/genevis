#!/usr/bin/env python3
"""Export explicitly selected v5 Assembly operations; never rewrite source graphs."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge.v5_projection import project_paper


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extractions", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--filename-template",
        choices=("{paper}.json", "{paper}-response.json"),
        default="{paper}.json",
        help="Source graph filename; raw response graphs need not be copied/renamed.",
    )
    args = parser.parse_args()
    selections = json.loads(args.selection.read_text())
    eligibility_path = (
        Path(__file__).resolve().parents[1]
        / "descriptors/knowledge_packs/assembly/parameter_eligibility.json"
    )
    eligibility = json.loads(eligibility_path.read_text())
    for selection in selections:
        if selection["stage"] == "assembly":
            case_id = f"{selection['paper']}:{selection['step']}"
            flag = selection.get("parameter_eligible", eligibility.get(case_id))
            if type(flag) is not bool:
                raise ValueError(f"missing parameter eligibility review: {case_id}")
            selection["parameter_eligible"] = flag
    papers = sorted({s["paper"] for s in selections if s["stage"] == "assembly"})
    prepared = {}
    coverage: Counter[str] = Counter()
    features: Counter[str] = Counter()
    for paper_id in papers:
        if not paper_id.isalnum():
            raise ValueError("paper IDs must be simple alphanumeric names")
        graph = json.loads(
            (
                args.extractions / args.filename_template.format(paper=paper_id)
            ).read_text()
        )
        paper = project_paper(paper_id, graph, selections)
        for case in paper.cases:
            coverage["cases"] += 1
            coverage["with_observations"] += bool(case.observations)
            coverage["with_reported_measurements"] += bool(
                case.content.get("reported_measurements")
            )
            coverage["with_outcomes"] += bool(case.content.get("outcomes"))
            coverage["with_normalization_notes"] += bool(
                case.content.get("normalization_notes")
            )
            features.update({o.name for o in case.observations})
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
    print(json.dumps({"coverage": coverage, "feature_case_counts": features}))
    if coverage["cases"] and not coverage["with_observations"]:
        print(
            "WARNING: entire corpus has no numeric material index; "
            "retrieval can only use lexical relevance.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
