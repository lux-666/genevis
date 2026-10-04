#!/usr/bin/env python3
"""Export explicitly selected v5 Assembly operations; never rewrite source graphs."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge.v5_projection import project_paper


def compare_export(prepared: dict[Path, bytes], existing: Path) -> dict:
    """Compare semantic content without changing a delivered case directory."""
    if not existing.is_dir():
        raise ValueError("comparison case directory does not exist")
    changes = []
    for destination, payload in sorted(prepared.items()):
        current = existing / destination.name
        if not current.exists():
            changes.append({"paper": destination.stem, "status": "missing"})
            continue
        if current.is_symlink() or not current.is_file():
            raise ValueError(f"comparison case must be a regular file: {current}")
        current_bytes = current.read_bytes()
        a, b = json.loads(payload), json.loads(current_bytes)
        if a == b:
            continue
        old = {c["case_id"]: c for c in b["cases"]}
        new = {c["case_id"]: c for c in a["cases"]}
        changes.append(
            {
                "paper": destination.stem,
                "status": "different",
                "export_sha256": sha256(payload).hexdigest(),
                "existing_sha256": sha256(current_bytes).hexdigest(),
                "added_cases": sorted(new.keys() - old.keys()),
                "removed_cases": sorted(old.keys() - new.keys()),
                "changed_cases": {
                    cid: sorted(
                        k
                        for k in new[cid].keys() | old[cid].keys()
                        if new[cid].get(k) != old[cid].get(k)
                    )
                    for cid in sorted(new.keys() & old.keys())
                    if new[cid] != old[cid]
                },
            }
        )
    unselected = sorted(
        p.name
        for p in existing.glob("*.json")
        if p.name not in {dest.name for dest in prepared}
    )
    return {
        "matches_selected_papers": not changes,
        "matches_complete_corpus": not changes and not unselected,
        "changes": changes,
        "unselected_existing_files": unselected,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extractions", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--compare-existing", type=Path, help="Audit only; never write case files"
    )
    parser.add_argument(
        "--audit-report",
        type=Path,
        help="Write provenance/comparison JSON outside case directories",
    )
    parser.add_argument(
        "--filename-template",
        choices=("{paper}.json", "{paper}-response.json"),
        default="{paper}.json",
        help="Source graph filename; raw response graphs need not be copied/renamed.",
    )
    args = parser.parse_args()
    if args.compare_existing and not args.audit_report:
        parser.error("--compare-existing requires --audit-report")
    if args.audit_report:
        report_path = args.audit_report.resolve()
        roots = [args.output.resolve(), args.extractions.resolve()]
        if args.compare_existing:
            roots.append(args.compare_existing.resolve())
        if any(report_path.is_relative_to(root) for root in roots):
            parser.error(
                "audit report must be outside source/output/comparison directories"
            )
        if report_path.exists() or report_path == args.selection.resolve():
            parser.error("audit report must be a new file")
    selection_bytes = args.selection.read_bytes()
    selections = json.loads(selection_bytes)
    eligibility_path = (
        Path(__file__).resolve().parents[1]
        / "descriptors/knowledge_packs/assembly/parameter_eligibility.json"
    )
    eligibility_bytes = eligibility_path.read_bytes()
    eligibility = json.loads(eligibility_bytes)
    for selection in selections:
        if selection["stage"] == "assembly":
            case_id = f"{selection['paper']}:{selection['step']}"
            flag = selection.get("parameter_eligible", eligibility.get(case_id))
            if type(flag) is not bool:
                raise ValueError(f"missing parameter eligibility review: {case_id}")
            selection["parameter_eligible"] = flag
    papers = sorted({s["paper"] for s in selections if s["stage"] == "assembly"})
    prepared = {}
    source_hashes = {}
    coverage: Counter[str] = Counter()
    features: Counter[str] = Counter()
    for paper_id in papers:
        if not paper_id.isalnum():
            raise ValueError("paper IDs must be simple alphanumeric names")
        source = args.extractions / args.filename_template.format(paper=paper_id)
        source_bytes = source.read_bytes()
        graph = json.loads(source_bytes)
        source_hashes[source.name] = sha256(source_bytes).hexdigest()
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
        if (
            not args.compare_existing
            and destination.exists()
            and destination.read_bytes() != payload
        ):
            raise ValueError(f"refusing to overwrite different case: {destination}")
        prepared[destination] = payload
    report = {
        "schema_version": 1,
        "selection_sha256": sha256(selection_bytes).hexdigest(),
        "eligibility_sha256": sha256(eligibility_bytes).hexdigest(),
        "source_sha256": source_hashes,
        "output_sha256": {p.name: sha256(b).hexdigest() for p, b in prepared.items()},
        "coverage": dict(coverage),
    }
    if args.compare_existing:
        report["comparison"] = compare_export(prepared, args.compare_existing)
    if args.audit_report:
        args.audit_report.parent.mkdir(parents=True, exist_ok=True)
        with args.audit_report.open("x") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.compare_existing:
        print(json.dumps(report["comparison"], ensure_ascii=False))
        if not report["comparison"]["matches_complete_corpus"]:
            raise SystemExit(1)
        return
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
