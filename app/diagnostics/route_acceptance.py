"""Read-only evidence audit for the complete HiFi finishing route.

Exit 0: full execution coverage; 2: incomplete/legitimately skipped coverage;
1: inconsistent evidence. None of these is a biological improvement verdict.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.configuration import load_descriptor_registry
from app.control.lifecycle import inspect_session
from app.control.workflow_plan import workflow_steps_from_run_config
from app.execution.evidence import load_execution_evidence, load_frozen_attempt
from app.review.persistence import load_review_request
from app.schemas.artifacts import ArtifactRef
from app.stages.base_polish.validation import load_comparison
from app.storage.artifacts import (
    artifact_ref_for_path,
    read_artifact_records,
    resolve_artifact_ref,
)
from app.storage.assembly_candidates import load_executed_candidate_bundle
from app.storage.frozen_inputs import validate_frozen_inputs
from app.storage.ledger import Ledger, project_stage_terminal_events
from app.storage.paths import SessionPaths
from app.workflow import ASSEMBLY_HANDOFF_PRODUCER_STAGES, recommended_workflow_template


def _ref(record: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef.model_validate(
        {key: record[key] for key in ArtifactRef.model_fields if key in record}
    )


def _json(paths: SessionPaths, ref: ArtifactRef) -> dict[str, Any]:
    payload = json.loads(
        resolve_artifact_ref(paths.run_dir, paths.session_id, ref).read_text()
    )
    if not isinstance(payload, dict):
        raise ValueError(f"expected object: {ref.path_ref}")
    return payload


def verify_delivery(paths: SessionPaths, row: dict[str, Any]) -> dict[str, Any]:
    refs = {item["role"]: _ref(item) for item in row["refs"]}
    candidate = load_executed_candidate_bundle(
        paths.run_dir, paths.session_id, refs["candidate_bundle_ref"]
    )
    if candidate.primary_output_ref != refs["primary_output_ref"]:
        raise ValueError("delivered candidate/output mismatch")
    return {
        "workflow_step_id": row["workflow_step_id"],
        "stage": row["stage"],
        "delivery_mode": row["summary"]["delivery_mode"],
        "candidate_id": candidate.bundle.candidate_id,
        "candidate_bundle_ref": candidate.ref.model_dump(mode="json"),
        "primary_output_ref": candidate.primary_output_ref.model_dump(mode="json"),
        "path": str(candidate.primary_output_path),
    }


def execution_coverage(
    state: dict[str, Any], rows: list[dict[str, Any]], records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Verify actual process records, keeping skips separate from execution."""
    registry = load_descriptor_registry()
    dispositions = [r for r in rows if r["event_type"] == "method_disposition_recorded"]
    methods = {r["summary"]["method_name"] for r in dispositions}
    if not methods <= set(registry.by_stage.get(state["current_stage"], [])):
        raise ValueError("method disposition belongs to another stage")
    roster = state["workflow_steps"][state["workflow_step_index"]].get(
        "selected_method_names", []
    )
    gaps = [f"missing method disposition: {name}" for name in set(roster) - methods]
    if state["current_stage"] == "assembly" and not methods:
        gaps.append("Assembly has no settled stage-owned method")
    result = []
    for row in dispositions:
        summary = row["summary"]
        name = summary["method_name"]
        attempts = []
        for record in records:
            if record["artifact_type"] != f"{state['current_stage']}.execution_result":
                continue
            if record["method_id"] != name:
                continue
            evidence = load_execution_evidence(
                state,
                _ref(record),
                expected_stage=state["current_stage"],
                expected_method_name=name,
            )
            paths = SessionPaths.from_run_dir(state["data_dir"])
            payload = _json(paths, _ref(record))
            frozen = load_frozen_attempt(
                state,
                payload["frozen_attempt_ref"],
                expected_stage=state["current_stage"],
                expected_method_name=name,
            )
            collections = [
                r
                for r in rows
                if r["event_type"] == "attempt_succeeded"
                and r["subject_id"] == evidence.attempt_id
            ]
            collected_refs = []
            for collection in collections:
                if (
                    collection["stage"] != state["current_stage"]
                    or collection["workflow_step_id"] != record["workflow_step_id"]
                    or collection["summary"].get("method_name") != name
                    or not collection["refs"]
                    or not evidence.success
                ):
                    raise ValueError(f"{name}: invalid collection evidence")
                for value in collection["refs"]:
                    ref = _ref(value)
                    resolve_artifact_ref(paths.run_dir, paths.session_id, ref)
                    collected_refs.append(ref.model_dump(mode="json"))
            attempts.append(
                {
                    "attempt_id": evidence.attempt_id,
                    "success": evidence.success,
                    "status": evidence.status,
                    "return_code": evidence.return_code,
                    "collected": bool(collections),
                    "collected_refs": collected_refs,
                    "evidence_ref": _ref(record).model_dump(mode="json"),
                    "candidate_id": frozen.metadata.get("candidate_id"),
                    "input_artifacts": [
                        item.artifact_ref.model_dump(mode="json")
                        for item in frozen.input_dependencies
                        if item.kind == "artifact"
                    ],
                    "evaluation_settings": {
                        key: frozen.metadata.get(key)
                        for key in (
                            "threads",
                            "reference_mode",
                            "reference_genome_path",
                            "lineage_path",
                            "lineage_version",
                            "mode",
                        )
                    },
                }
            )
        local = registry.methods[name].planner.execution_kind == "local"
        executed = any(a["success"] and a["collected"] for a in attempts)
        if local and summary["disposition"] == "completed" and not executed:
            raise ValueError(f"{name}: completed without verified process/collection")
        if local and (not executed or summary["disposition"] != "completed"):
            gaps.append(f"{name}: {summary['disposition']}: {summary.get('reason')}")
        result.append(
            {**summary, "processes": attempts, "execution_verified": executed}
        )
    return result, gaps


def final_comparison(
    paths: SessionPaths,
    records: list[dict[str, Any]],
    final_step_id: str,
    request_ref: ArtifactRef,
) -> dict[str, Any]:
    polish_records = [
        r for r in records if r["artifact_type"] == "base_polish.comparison"
    ]
    if len(polish_records) != 1:
        raise ValueError(
            "complete route requires one source-bound polishing comparison"
        )
    comparison_ref = _ref(polish_records[0])
    report = _json(paths, comparison_ref)
    pair = [
        ArtifactRef.model_validate(report[key])
        for key in ("source_candidate_ref", "polished_candidate_ref")
    ]
    load_comparison(
        paths.run_dir, paths.session_id, comparison_ref, candidate_refs=pair
    )
    ids = {
        load_executed_candidate_bundle(
            paths.run_dir, paths.session_id, ref
        ).bundle.candidate_id
        for ref in pair
    }
    request = load_review_request(
        paths.run_dir, request_ref, session_id=paths.session_id
    )
    if (
        request["workflow_step_id"] != final_step_id
        or request["gate_id"] != "assembly_review"
    ):
        raise ValueError("final Review request is bound to another occurrence")
    packet_ref = artifact_ref_for_path(
        paths.run_dir,
        paths.session_id,
        paths.resolve_ref(request["payload"]["report_refs"]["review_packet"]),
        expected_type="assembly_review.review_packet",
    )
    packet = _json(paths, packet_ref)
    rows = packet["candidates"]
    if {c["candidate_id"] for c in rows} != ids or len(rows) != 2:
        raise ValueError("final Review did not evaluate exactly the polishing pair")
    if (packet.get("polishing_comparison") or {}).get(
        "report_ref"
    ) != comparison_ref.model_dump(mode="json"):
        raise ValueError("final Review uses another polishing comparison")
    for candidate in rows:
        if not {"quast", "busco"} <= set(candidate["signals"]["available"]):
            raise ValueError(
                "final Review requires QUAST and BUSCO for BOTH candidates"
            )
        for signal in ("quast", "busco"):
            refs = candidate["metric_refs_by_signal"].get(signal, [])
            if not refs or not candidate["metrics"].get(signal):
                raise ValueError(f"final Review {signal} metrics are missing")
            for value in refs:
                ref = artifact_ref_for_path(paths.run_dir, paths.session_id, value)
                resolve_artifact_ref(paths.run_dir, paths.session_id, ref)
    return {
        "comparison_ref": comparison_ref.model_dump(mode="json"),
        "review_packet_ref": packet_ref.model_dump(mode="json"),
        "candidates": rows,
        "polish_delta": report["delta"],
        "independent_qv": None,
        "quality_verdict": "no_automated_improvement_verdict; see final delivery",
    }


def audit_route(run_dir: Path) -> dict[str, Any]:
    paths = SessionPaths.from_run_dir(run_dir)
    frozen = validate_frozen_inputs(paths)
    steps = workflow_steps_from_run_config(frozen.run_config)
    if frozen.run_config.requested_stage_names() != recommended_workflow_template(
        "t2t"
    ):
        raise ValueError(
            "not the complete finishing route; use hifi_complete templates"
        )
    snapshot = inspect_session(paths.session_id, paths.run_dir)
    errors = {
        key: snapshot[key]
        for key in (
            "error",
            "review_status_integrity_errors",
            "terminal_integrity",
            "stage_terminal_integrity",
            "workflow_progress_integrity",
            "final_assembly_integrity_error",
        )
        if snapshot.get(key)
    }
    if errors:
        raise ValueError(f"session integrity errors: {errors}")
    records = read_artifact_records(paths.run_dir, paths.session_id)
    rows = Ledger(paths.run_dir).read_event_rows(paths.session_id)
    terminal_sequence = (snapshot.get("latest_terminal_event") or {}).get("sequence")
    if terminal_sequence is not None:
        rows = [r for r in rows if r["sequence"] <= terminal_sequence]
    terminals, conflicts = project_stage_terminal_events(rows)
    if conflicts:
        raise ValueError(f"conflicting occurrence outcomes: {conflicts}")
    occurrences = []
    previous_primary: dict[str, Any] | None = None
    gaps: list[str] = []
    for index, step in enumerate(steps):
        owned = [r for r in rows if r.get("workflow_step_id") == step["step_id"]]
        indexed = [r for r in records if r.get("workflow_step_id") == step["step_id"]]
        state = {
            "data_dir": str(paths.run_dir),
            "session_id": paths.session_id,
            "workflow_steps": steps,
            "workflow_step_index": index,
            "current_stage": step["stage"],
        }
        methods, missing = execution_coverage(state, owned, indexed)
        gaps.extend(f"{step['step_id']}: {item}" for item in missing)
        terminal = terminals.get((step["step_id"], step["stage"]), {})
        outcome = terminal.get("event_type", "not_terminal")
        if outcome not in {"stage_completed", "stage_skipped"}:
            gaps.append(f"{step['step_id']}: {outcome}")
        deliveries = [
            verify_delivery(paths, r)
            for r in owned
            if r["event_type"] == "resource_delivered"
        ]
        if previous_primary and step["stage"] in ASSEMBLY_HANDOFF_PRODUCER_STAGES - {
            "assembly",
            "inventory",
        }:
            processes = [p for m in methods for p in m["processes"] if p["success"]]
            if processes and not any(
                previous_primary in p["input_artifacts"] for p in processes
            ):
                raise ValueError(
                    f"{step['step_id']}: no execution consumes preceding assembly"
                )
            if (
                deliveries
                and deliveries[0]["delivery_mode"] == "reused"
                and step["stage"] != "assembly_review"
            ):
                if deliveries[0]["primary_output_ref"] != previous_primary:
                    raise ValueError(
                        f"{step['step_id']}: reused delivery changed assembly"
                    )
        if step["stage"] in ASSEMBLY_HANDOFF_PRODUCER_STAGES - {"inventory"}:
            if outcome == "stage_completed" and len(deliveries) != 1:
                raise ValueError(
                    f"{step['step_id']}: missing or ambiguous assembly handoff"
                )
        occurrences.append(
            {
                "step_id": step["step_id"],
                "stage": step["stage"],
                "outcome": outcome,
                "methods": methods,
                "deliveries": deliveries,
            }
        )
        if deliveries:
            previous_primary = deliveries[0]["primary_output_ref"]
    complete = snapshot["supervision_state"] == "completed"
    if complete and not snapshot.get("final_assembly"):
        raise ValueError("completed session has no verified final assembly")
    quality = (
        final_comparison(
            paths,
            records,
            steps[-1]["step_id"],
            _ref(snapshot["latest_review_request_artifact"]),
        )
        if complete
        else None
    )
    if quality:
        pair_ids = {c["candidate_id"] for c in quality["candidates"]}
        for method in occurrences[-1]["methods"]:
            successful = [
                p for p in method["processes"] if p["success"] and p["collected"]
            ]
            if {p["candidate_id"] for p in successful} != pair_ids:
                raise ValueError("final evaluator has not executed for both candidates")
            settings = {
                json.dumps(p["evaluation_settings"], sort_keys=True) for p in successful
            }
            if len(settings) != 1:
                raise ValueError("final candidates used different evaluator settings")
        if (
            not occurrences[-1]["deliveries"]
            or occurrences[-1]["deliveries"][0]["candidate_id"] not in pair_ids
        ):
            raise ValueError("final output is not one of the evaluated candidates")
    if not complete:
        gaps.append(f"session: {snapshot['supervision_state']}")
    return {
        "schema_version": 1,
        "session_id": paths.session_id,
        "status": "complete_execution_coverage"
        if not gaps
        else "incomplete_execution_coverage",
        "session_state": snapshot["supervision_state"],
        "occurrences": occurrences,
        "coverage_gaps": gaps,
        "final_assembly": snapshot.get("final_assembly"),
        "quality_comparison": quality,
        "limitations": [
            "No claim of independent QV or biological improvement.",
            "Legitimate skips are not code failures or proof of tool execution.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = audit_route(args.run_dir)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"status": "invalid_evidence", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "complete_execution_coverage" else 2


if __name__ == "__main__":
    raise SystemExit(main())
