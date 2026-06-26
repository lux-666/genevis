"""Evidence-driven telomere inspection and conservative T2T patching."""

from __future__ import annotations

import csv
import gzip
import json
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from app.control.ledger import Ledger
from app.control.lifecycle import ResumeValidationError
from app.execution.artifacts import append_artifact_records
from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.graph.routing import next_stage_name
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.schemas.artifacts import (
    T2T_ASSEMBLY_TELOMERE_SCAN,
    T2TReviewDecisionArtifact,
    T2T_EXTENSION_CANDIDATES,
    T2T_FINISHING_REPORT,
    T2T_PATCHED_ASSEMBLY,
    T2T_PATCH_RECORDS,
    T2T_READS_TELOMERE_SCAN,
    T2T_STATUS_SUMMARY,
    T2T_TELOMERIC_READS,
    ArtifactMeta,
    T2TFinishingBundle,
)
from app.schemas.descriptors import DescriptorRegistry, FrozenAttemptSpec
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir

T2T_ALIGNMENT_METHOD = "t2t.alignment.minimap2"
T2T_CONTRACT_METHOD = "t2t.contract"
DEFAULT_THRESHOLDS = {
    "min_motif_copies": 4,
    "terminal_window_bp": 10_000,
    "min_mapq": 20,
    "max_target_terminal_distance_bp": 1_000,
    "min_overlap_bp": 500,
    "min_supporting_reads": 2,
}


@dataclass(frozen=True)
class PafRecord:
    query: str
    query_length: int
    query_start: int
    query_end: int
    strand: str
    target: str
    target_length: int
    target_start: int
    target_end: int
    mapq: int

    @property
    def overlap_bp(self) -> int:
        return self.query_end - self.query_start


def _output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("t2t") or {})


def _update(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["t2t"] = {**_output(state), **payload}
    return {"stage_outputs": outputs}


def _fail(state: TopLevelState, reason: str) -> dict:
    return _update(state, {"status": "failed", "reason": reason})


def enter_t2t(state: TopLevelState) -> dict:
    """Mark T2T finishing as the active stage."""
    return {"supervision_state": "running_stage"}


def resolve_t2t_inputs(state: TopLevelState) -> dict:
    """Resolve the current assembly and the preferred ONT evidence source."""
    existing = _output(state)
    config = _config(existing)
    assembly_ref, source_stage = _assembly_ref(state, config.get("assembly_fasta_ref"))
    if not assembly_ref:
        return _fail(state, "missing final assembly ref for t2t")
    assembly_path = Path(assembly_ref)
    if not assembly_path.exists() or not assembly_path.is_file() or not _looks_like_fasta(assembly_path):
        return _fail(state, f"t2t input assembly is not a valid FASTA: {assembly_ref}")

    reads_ref, reads_source = _ont_reads_ref(state, config.get("ont_reads_ref"))
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "t2t" / session_id / "t2t_finishing"
    work_dir = data_dir / "work" / "t2t" / session_id / "t2t_finishing"
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    return _update(
        state,
        {
            "status": "resolved",
            "mode": config["mode"],
            "telomere_motif": config["telomere_motif"],
            "thresholds": config["thresholds"],
            "chromosome_list_ref": config.get("chromosome_list_ref"),
            "input_assembly_ref": str(assembly_path),
            "source_stage": source_stage,
            "ont_reads_ref": reads_ref,
            "ont_reads_source": reads_source,
            "execution_enabled": existing.get("execution_mode") == "local",
            "out_dir": str(out_dir),
            "work_dir": str(work_dir),
            "final_assembly_for_annotation": str(assembly_path),
            "notes": [],
        },
    )


def scan_assembly_telomeres(state: TopLevelState) -> dict:
    """Write per-contig terminal telomere evidence without changing the FASTA."""
    out = _output(state)
    if out.get("status") != "resolved":
        return {}
    try:
        rows = list(_scan_fasta_ends(Path(out["input_assembly_ref"]), out["telomere_motif"], out["thresholds"]))
    except (OSError, ValueError) as exc:
        return _fail(state, f"t2t assembly telomere scan failed: {exc}")
    scan_path = Path(out["out_dir"]) / "assembly_telomere_scan.tsv"
    _write_tsv(scan_path, rows, _ASSEMBLY_SCAN_FIELDS)
    chromosome_names = _load_chromosome_list(out.get("chromosome_list_ref"))
    warnings = list(out.get("notes") or [])
    if chromosome_names:
        unknown = sorted(chromosome_names.difference({row["sequence"] for row in rows}))
        if unknown:
            warnings.append(f"chromosome_list_missing_from_assembly:{','.join(unknown)}")
    if len(rows) > 100:
        warnings.append("assembly_has_more_than_100_sequences; chromosome-level suitability is uncertain")
    missing_ends = sum(int(row["left_has_signal"] == "false") + int(row["right_has_signal"] == "false") for row in rows)
    return _update(
        state,
        {
            "status": "assembly_scanned",
            "assembly_telomere_scan_ref": str(scan_path),
            "assembly_sequence_count": len(rows),
            "assembly_missing_end_count": missing_ends,
            "notes": warnings,
        },
    )


def prepare_t2t_reads(state: TopLevelState) -> dict:
    """Validate the resolved ONT path before scanning or mapping it."""
    out = _output(state)
    if out.get("status") != "assembly_scanned":
        return {}
    reads_ref = out.get("ont_reads_ref")
    if not reads_ref:
        return _skip_no_reads(state, out, "no_ont_long_reads")
    reads_path = Path(str(reads_ref))
    if not reads_path.exists() or not reads_path.is_file():
        return _skip_no_reads(state, out, f"ont_reads_not_found:{reads_path}")
    try:
        next(_iter_sequences(reads_path))
    except (OSError, StopIteration, ValueError) as exc:
        return _skip_no_reads(state, out, f"invalid_ont_reads:{exc}")
    return _update(state, {"status": "reads_prepared", "ont_reads_path": str(reads_path)})


def scan_read_telomeres(state: TopLevelState) -> dict:
    """Stream ONT reads and retain only terminal-telomeric reads for evidence."""
    out = _output(state)
    if out.get("status") != "reads_prepared":
        return {}
    reads_scan = Path(out["out_dir"]) / "reads_telomere_scan.tsv"
    telomeric_tsv = Path(out["out_dir"]) / "telomeric_reads.tsv"
    telomeric_fasta = Path(out["work_dir"]) / "telomeric_reads.fa"
    read_count = 0
    telomeric_read_count = 0
    try:
        with (
            reads_scan.open("w", encoding="utf-8", newline="") as reads_handle,
            telomeric_tsv.open("w", encoding="utf-8", newline="") as telomeric_handle,
            telomeric_fasta.open("w", encoding="utf-8") as fasta_handle,
        ):
            reads_writer = csv.DictWriter(reads_handle, fieldnames=_READ_SCAN_FIELDS, delimiter="\t", extrasaction="ignore")
            telomeric_writer = csv.DictWriter(telomeric_handle, fieldnames=_READ_SCAN_FIELDS, delimiter="\t", extrasaction="ignore")
            reads_writer.writeheader()
            telomeric_writer.writeheader()
            for name, sequence in _iter_sequences(Path(out["ont_reads_path"])):
                left, right = _terminal_signal(sequence, out["telomere_motif"], out["thresholds"])
                row = {
                    "read_id": name,
                    "length": str(len(sequence)),
                    "left_motif_copies": str(left),
                    "right_motif_copies": str(right),
                    "left_has_signal": str(left >= out["thresholds"]["min_motif_copies"]).lower(),
                    "right_has_signal": str(right >= out["thresholds"]["min_motif_copies"]).lower(),
                }
                reads_writer.writerow(row)
                read_count += 1
                if row["left_has_signal"] == "true" or row["right_has_signal"] == "true":
                    telomeric_writer.writerow(row)
                    _write_fasta_record(fasta_handle, name, sequence)
                    telomeric_read_count += 1
    except (OSError, ValueError) as exc:
        return _fail(state, f"t2t read telomere scan failed: {exc}")
    return _update(
        state,
        {
            "status": "reads_scanned",
            "reads_telomere_scan_ref": str(reads_scan),
            "telomeric_reads_ref": str(telomeric_tsv),
            "telomeric_reads_fasta_ref": str(telomeric_fasta),
            "read_count": read_count,
            "telomeric_read_count": telomeric_read_count,
        },
    )


def freeze_t2t_alignment(state: TopLevelState) -> dict:
    """Freeze a minimap2 PAF alignment attempt with normal env preflight."""
    out = _output(state)
    if out.get("status") != "reads_scanned":
        return {}
    if not out.get("telomeric_read_count"):
        candidates_path = Path(out["out_dir"]) / "telomere_extension_candidates.tsv"
        _write_tsv(candidates_path, [], _CANDIDATE_FIELDS)
        return _update(
            state,
            {
                "status": "alignment_skipped",
                "candidate_skip_reason": "no_telomeric_reads",
                "telomere_extension_candidates_ref": str(candidates_path),
                "candidate_count": 0,
            },
        )
    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    if T2T_ALIGNMENT_METHOD not in registry.methods:
        return _fail(state, f"t2t descriptor missing: {T2T_ALIGNMENT_METHOD}")
    env_spec, _packages, blocked = resolve_method_env(
        out,
        stage="t2t",
        method_name=T2T_ALIGNMENT_METHOD,
        session_id=state["session_id"],
        data_dir=absolute_data_dir(state),
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked:
        return _update(state, blocked)
    work_dir = Path(out["work_dir"])
    paf_path = work_dir / "ont_to_assembly.paf"
    script_path = work_dir / "run_minimap2_t2t.sh"
    alignment_reads_ref = out.get("telomeric_reads_fasta_ref")
    if not alignment_reads_ref:
        return _fail(state, "t2t telomeric read FASTA is missing after read scan")
    script_path.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"minimap2 -x map-ont --secondary=no {shlex.quote(out['input_assembly_ref'])} {shlex.quote(str(alignment_reads_ref))} > {shlex.quote(str(paf_path))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    attempt = build_frozen_attempt(
        attempt_id=f"{state['session_id']}-t2t-minimap2",
        session_id=state["session_id"],
        stage="t2t",
        method_name=T2T_ALIGNMENT_METHOD,
        script_path=script_path,
        working_dir=work_dir,
        expected_outputs=[paf_path],
        env=out.get("env"),
        env_spec=env_spec,
        metadata={"paf_path": str(paf_path), "assembly_ref": out["input_assembly_ref"], "reads_ref": str(alignment_reads_ref)},
    )
    return _update(state, {"status": "alignment_frozen", "alignment_attempt": attempt.model_dump(mode="json"), "alignment_paf_ref": str(paf_path)})


def run_t2t_alignment(state: TopLevelState) -> dict:
    """Run the frozen minimap2 evidence alignment."""
    out = _output(state)
    if out.get("status") != "alignment_frozen":
        return {}
    attempt = FrozenAttemptSpec.model_validate(out["alignment_attempt"])
    outcome = run_frozen_attempt(
        attempt,
        data_dir=absolute_data_dir(state),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label="t2t minimap2",
    )
    result = outcome.result.model_dump(mode="json")
    if not outcome.success:
        return _fail(state, outcome.error)
    return _update(state, {"status": "alignment_ran", "alignment_result": result})


def build_t2t_candidates(state: TopLevelState) -> dict:
    """Combine terminal signals and PAF placements into patch candidates."""
    out = _output(state)
    if out.get("status") not in {"alignment_ran", "candidates_built"}:
        return {}
    if out.get("status") == "candidates_built":
        return {}
    candidates_path = Path(out["out_dir"]) / "telomere_extension_candidates.tsv"
    try:
        assembly_ends = {row["sequence"]: row for row in _read_tsv(Path(out["assembly_telomere_scan_ref"]))}
        read_signals = {row["read_id"]: row for row in _read_tsv(Path(out["telomeric_reads_ref"]))}
        read_sequences = dict(_iter_sequences(Path(out["telomeric_reads_fasta_ref"])))
        allowed = _load_chromosome_list(out.get("chromosome_list_ref"))
        raw = _candidate_rows(
            Path(out["alignment_paf_ref"]),
            assembly_ends,
            read_signals,
            read_sequences,
            out["thresholds"],
            out["telomere_motif"],
            allowed,
            out["mode"],
        )
    except (OSError, ValueError) as exc:
        return _fail(state, f"t2t candidate construction failed: {exc}")
    _write_tsv(candidates_path, raw, _CANDIDATE_FIELDS)
    selected = sum(row["decision"] == "auto_patch" for row in raw)
    proposed = sum(row["decision"] == "proposed" for row in raw)
    return _update(
        state,
        {
            "status": "candidates_built",
            "telomere_extension_candidates_ref": str(candidates_path),
            "candidate_count": len(raw),
            "selected_patch_count": selected,
            "proposed_candidate_count": proposed,
        },
    )


def apply_t2t_patches(state: TopLevelState) -> dict:
    """Materialize a new FASTA only for validated, auto-selected candidates."""
    out = _output(state)
    if out.get("status") != "candidates_built":
        return {}
    if out.get("mode") != "auto_patch":
        return _update(state, {"status": "patch_not_applied", "patch_reason": "no_auto_patch_candidates"})
    materialized = _materialize_t2t_patches(out, allowed_decisions={"auto_patch"})
    if not materialized:
        return _update(state, {"status": "patch_not_applied", "patch_reason": "selected_read_sequence_unavailable"})
    return _update(
        state,
        {
            "status": "patched",
            "patched_assembly_ref": materialized["patched_assembly_ref"],
            "patch_records_ref": materialized["patch_records_ref"],
            "final_assembly_for_annotation": materialized["patched_assembly_ref"],
        },
    )


def validate_patched_assembly(state: TopLevelState) -> dict:
    """Rescan patched ends and preserve the validation table as evidence."""
    out = _output(state)
    if out.get("status") != "patched":
        return {}
    try:
        rows = list(_scan_fasta_ends(Path(out["patched_assembly_ref"]), out["telomere_motif"], out["thresholds"]))
    except (OSError, ValueError) as exc:
        return _fail(state, f"t2t patched assembly validation failed: {exc}")
    path = Path(out["out_dir"]) / "patched_telomere_scan.tsv"
    _write_tsv(path, rows, _ASSEMBLY_SCAN_FIELDS)
    return _update(state, {"status": "patched_validated", "patched_telomere_scan_ref": str(path)})


def write_t2t_report(state: TopLevelState) -> dict:
    """Write the compact operator report and register all materialized evidence."""
    out = _output(state)
    if out.get("status") == "failed":
        return {}
    summary_path = Path(out["out_dir"]) / "telomere_status_summary.tsv"
    scan_ref = out.get("patched_telomere_scan_ref") or out.get("assembly_telomere_scan_ref")
    rows = _read_tsv(Path(scan_ref)) if scan_ref and Path(scan_ref).exists() else []
    summary = [
        {
            "sequence": row["sequence"],
            "left_telomere": row["left_has_signal"],
            "right_telomere": row["right_has_signal"],
            "status": "complete" if row["left_has_signal"] == "true" and row["right_has_signal"] == "true" else "missing_signal",
        }
        for row in rows
    ]
    _write_tsv(summary_path, summary, ["sequence", "left_telomere", "right_telomere", "status"])
    report_path = Path(out["out_dir"]) / "t2t_finishing_report.md"
    report_path.write_text(_report_markdown(out, summary), encoding="utf-8")
    final_status = _final_status(out, summary)
    artifacts = _register_artifacts(state, out, summary_path, report_path)
    bundle_path = Path(out["out_dir"]) / "t2t_finishing_bundle.json"
    bundle = T2TFinishingBundle(
        session_id=state["session_id"],
        status=final_status,
        input_assembly_ref=out["input_assembly_ref"],
        final_assembly_for_annotation=out.get("final_assembly_for_annotation") or out["input_assembly_ref"],
        ont_reads_ref=out.get("ont_reads_ref"),
        mode=out.get("mode", "auto_patch"),
        patched_assembly_ref=out.get("patched_assembly_ref"),
        artifact_refs={path.name: str(path) for path in artifacts},
        notes=list(out.get("notes") or []),
        metadata={"source_stage": out.get("source_stage"), "execution_enabled": out.get("execution_enabled")},
    )
    bundle_path.write_text(json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    bundle_artifact = ArtifactMeta.from_file(bundle_path, stage="t2t", method_name=T2T_CONTRACT_METHOD, artifact_type="t2t.bundle")
    append_artifact_records(absolute_data_dir(state), state["session_id"], [bundle_artifact])
    payload = {
        "status": final_status,
        "telomere_status_summary_ref": str(summary_path),
        "t2t_finishing_report_ref": str(report_path),
        "bundle_ref": str(bundle_path),
        "artifact_refs": [str(path) for path in artifacts] + [str(bundle_path)],
        "final_assembly_for_annotation": out.get("final_assembly_for_annotation") or out["input_assembly_ref"],
    }
    if final_status != "blocked_human":
        return _update(state, payload)
    request_update = _block_for_t2t_review(
        state,
        out,
        report_path=report_path,
        summary_path=summary_path,
        bundle_path=bundle_path,
    )
    outputs = dict(request_update.get("stage_outputs") or {})
    outputs["t2t"] = {**dict(outputs.get("t2t") or {}), **payload}
    return {**request_update, "stage_outputs": outputs}


def commit_t2t(state: TopLevelState) -> dict:
    """Advance to annotation with the patched or original assembly reference."""
    return {
        "current_stage": next_stage_name("t2t"),
        "supervision_state": "running_stage",
        "route_trace": [*state.get("route_trace", []), "t2t_stage"],
    }


def fail_t2t(state: TopLevelState) -> dict:
    """End the workflow only for invalid inputs or terminal tool failures."""
    out = _output(state)
    return {
        "current_stage": "t2t",
        "supervision_state": "failed_terminal",
        "last_error": out.get("reason", "t2t failed"),
    }


def apply_t2t_review_decision(state: TopLevelState, request: ResumeReviewRequest) -> dict:
    """Apply a human T2T review decision to a blocked state."""
    if request.stage != "t2t":
        raise ResumeValidationError("t2t review resume requires stage t2t")
    out = _output(state)
    allowed = {"accept_selected_candidates", "continue_without_patch", "stop_pipeline"}
    if request.decision not in allowed:
        raise ResumeValidationError(f"unsupported t2t review decision: {request.decision!r}")
    if request.decision == "accept_selected_candidates" and not request.candidate_ids:
        raise ResumeValidationError("t2t accept_selected_candidates requires candidate_ids")

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    candidate_rows = _read_tsv(Path(out["telomere_extension_candidates_ref"]))
    selectable = {
        row["candidate_id"]: row
        for row in candidate_rows
        if row.get("candidate_id") and row.get("decision") in {"proposed", "auto_patch"}
    }
    if request.decision == "accept_selected_candidates":
        unknown = sorted(set(request.candidate_ids).difference(selectable))
        if unknown:
            raise ResumeValidationError(f"unknown t2t candidate_ids: {', '.join(unknown)}")
        materialized = _materialize_t2t_patches(out, selected_candidate_ids=set(request.candidate_ids), allowed_decisions={"proposed", "auto_patch"})
        if not materialized:
            raise ResumeValidationError("t2t selected candidates could not be materialized into a patched assembly")
        final_assembly_for_annotation = materialized["patched_assembly_ref"]
        patched_assembly_ref = materialized["patched_assembly_ref"]
        patch_records_ref = materialized["patch_records_ref"]
        append_artifact_records(
            data_dir,
            session_id,
            [
                ArtifactMeta.from_file(Path(patched_assembly_ref), stage="t2t", method_name=T2T_CONTRACT_METHOD, artifact_type=T2T_PATCHED_ASSEMBLY),
                ArtifactMeta.from_file(Path(patch_records_ref), stage="t2t", method_name=T2T_CONTRACT_METHOD, artifact_type=T2T_PATCH_RECORDS),
            ],
        )
        status = "completed"
    else:
        final_assembly_for_annotation = out["input_assembly_ref"]
        patched_assembly_ref = None
        patch_records_ref = out.get("patch_records_ref")
        status = "failed_terminal" if request.decision == "stop_pipeline" else "completed"

    decision_ref = Path(out["out_dir"]) / "t2t_review_decision.json"
    decision_payload = T2TReviewDecisionArtifact(
        session_id=session_id,
        decision=request.decision,
        candidate_ids=list(request.candidate_ids),
        input_assembly_ref=out["input_assembly_ref"],
        final_assembly_for_annotation=str(final_assembly_for_annotation),
        patched_assembly_ref=patched_assembly_ref,
        notes=list(request.notes),
        metadata=request.metadata,
    )
    decision_ref.write_text(json.dumps(decision_payload.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        decision_ref,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type="t2t.review_decision",
        metadata={"decision": request.decision},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    Ledger(data_dir).record_review_decision(
        session_id,
        "t2t",
        decision=request.decision,
        notes=list(request.notes),
        artifact_ref=str(artifact.file_path),
        metadata=request.metadata,
        decided_by=request.decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
    )

    outputs = dict(state.get("stage_outputs") or {})
    outputs["t2t"] = {
        **out,
        "status": status,
        "decision": request.decision,
        "selected_candidate_ids": list(request.candidate_ids),
        "patched_assembly_ref": patched_assembly_ref,
        "patch_records_ref": patch_records_ref,
        "final_assembly_for_annotation": str(final_assembly_for_annotation),
        "review_decision_artifact_ref": str(artifact.file_path),
        "notes": list(request.notes),
        "decided_by": request.decided_by,
        "rationale_artifact_ref": request.rationale_artifact_ref,
    }
    if request.decision == "stop_pipeline":
        return {
            "current_stage": "t2t",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "stage_outputs": outputs,
            "last_error": "t2t stopped by human review decision",
        }
    updated_state = {**state, "stage_outputs": outputs, "active_interrupt": None, "supervision_state": "running_stage"}
    commit_update = commit_t2t(updated_state)
    return {**commit_update, "active_interrupt": None, "stage_outputs": outputs}


def _config(out: dict[str, Any]) -> dict[str, Any]:
    thresholds = {**DEFAULT_THRESHOLDS, **dict(out.get("thresholds") or {})}
    return {
        "mode": out.get("mode", "auto_patch"),
        "telomere_motif": str(out.get("telomere_motif", "TTAGGG")).upper(),
        "thresholds": thresholds,
        "chromosome_list_ref": out.get("chromosome_list_ref"),
        "assembly_fasta_ref": out.get("assembly_fasta_ref"),
        "ont_reads_ref": out.get("ont_reads_ref"),
    }


def _assembly_ref(state: TopLevelState, override: str | None) -> tuple[str | None, str]:
    if override:
        return str(override), "run_config"
    outputs = state.get("stage_outputs") or {}
    hic = outputs.get("hic_scaffolding") or {}
    for key in ("final_assembly_for_t2t_or_annotation", "final_assembly_for_annotation", "chromosome_scaffolded_assembly_candidate_ref"):
        if hic.get(key):
            return str(hic[key]), "hic_scaffolding"
    review = outputs.get("assembly_review") or {}
    for key in ("final_assembly_for_scaffolding_or_annotation", "final_assembly_ref"):
        if review.get(key):
            return str(review[key]), "assembly_review"
    return None, "missing"


def _ont_reads_ref(state: TopLevelState, override: str | None) -> tuple[str | None, str]:
    if override:
        return str(override), "run_config"
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref or not Path(registry_ref).exists():
        return None, "missing"
    try:
        assets = json.loads(Path(registry_ref).read_text(encoding="utf-8")).get("assets", [])
    except (OSError, json.JSONDecodeError):
        return None, "missing"
    for asset_type in ("clean_long_reads", "raw_long_reads"):
        for asset in assets:
            if asset.get("asset_type") == asset_type and asset.get("path"):
                return str(asset["path"]), asset_type
    return None, "missing"


def _skip_no_reads(state: TopLevelState, out: dict[str, Any], reason: str) -> dict:
    reads_scan = Path(out["out_dir"]) / "reads_telomere_scan.tsv"
    telomeric_tsv = Path(out["out_dir"]) / "telomeric_reads.tsv"
    candidates = Path(out["out_dir"]) / "telomere_extension_candidates.tsv"
    _write_tsv(reads_scan, [], _READ_SCAN_FIELDS)
    _write_tsv(telomeric_tsv, [], _READ_SCAN_FIELDS)
    _write_tsv(candidates, [], _CANDIDATE_FIELDS)
    return _update(
        state,
        {
            "status": "skipped_no_long_reads",
            "skip_reason": reason,
            "reads_telomere_scan_ref": str(reads_scan),
            "telomeric_reads_ref": str(telomeric_tsv),
            "telomere_extension_candidates_ref": str(candidates),
            "candidate_count": 0,
        },
    )


def _looks_like_fasta(path: Path) -> bool:
    try:
        with _open_text(path) as handle:
            return handle.read(1) == ">"
    except OSError:
        return False


def _open_text(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open("r", encoding="utf-8")


def _iter_sequences(path: Path) -> Iterator[tuple[str, str]]:
    with _open_text(path) as handle:
        first = handle.readline()
        if not first:
            raise ValueError("empty sequence file")
        if first.startswith(">"):
            name = first[1:].strip().split()[0]
            sequence: list[str] = []
            for line in handle:
                if line.startswith(">"):
                    if not name:
                        raise ValueError("FASTA record has no identifier")
                    yield name, "".join(sequence).upper()
                    name = line[1:].strip().split()[0]
                    sequence = []
                else:
                    sequence.append(line.strip())
            if not name:
                raise ValueError("FASTA record has no identifier")
            yield name, "".join(sequence).upper()
            return
        if not first.startswith("@"):
            raise ValueError("expected FASTA or FASTQ")
        header = first
        while header:
            name = header[1:].strip().split()[0]
            sequence = handle.readline().strip()
            plus = handle.readline()
            quality = handle.readline()
            if not name or not sequence or not plus.startswith("+") or not quality:
                raise ValueError("invalid FASTQ record")
            yield name, sequence.upper()
            header = handle.readline()
            if header and not header.startswith("@"):
                raise ValueError("invalid FASTQ header")


def _scan_fasta_ends(path: Path, motif: str, thresholds: dict[str, int]) -> Iterable[dict[str, str]]:
    for name, sequence in _iter_sequences(path):
        left, right = _terminal_signal(sequence, motif, thresholds)
        yield {
            "sequence": name,
            "length": str(len(sequence)),
            "left_motif_copies": str(left),
            "right_motif_copies": str(right),
            "left_has_signal": str(left >= thresholds["min_motif_copies"]).lower(),
            "right_has_signal": str(right >= thresholds["min_motif_copies"]).lower(),
        }


def _terminal_signal(sequence: str, motif: str, thresholds: dict[str, int]) -> tuple[int, int]:
    window = min(len(sequence), int(thresholds["terminal_window_bp"]))
    return _max_motif_copies(sequence[:window], motif), _max_motif_copies(sequence[-window:], motif)


def _max_motif_copies(sequence: str, motif: str) -> int:
    alternatives = [re.escape(motif), re.escape(_reverse_complement(motif))]
    match_lengths = [len(match.group(0)) // len(motif) for match in re.finditer(f"(?:{'|'.join(alternatives)})+", sequence)]
    return max(match_lengths, default=0)


def _reverse_complement(sequence: str) -> str:
    return sequence.upper().translate(str.maketrans("ACGTN", "TGCAN"))[::-1]


def _candidate_rows(
    paf_path: Path,
    assembly_ends: dict[str, dict[str, str]],
    read_signals: dict[str, dict[str, str]],
    read_sequences: dict[str, str],
    thresholds: dict[str, int],
    motif: str,
    allowed_sequences: set[str] | None,
    mode: str,
) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for paf in _iter_paf(paf_path):
        signal = read_signals.get(paf.query)
        sequence = read_sequences.get(paf.query)
        target = assembly_ends.get(paf.target)
        if not signal or not sequence or not target or (allowed_sequences and paf.target not in allowed_sequences):
            continue
        for read_end in ("left", "right"):
            if signal.get(f"{read_end}_has_signal") != "true":
                continue
            target_end = read_end if paf.strand == "+" else ("right" if read_end == "left" else "left")
            if target.get(f"{target_end}_has_signal") == "true":
                continue
            target_distance = paf.target_start if target_end == "left" else paf.target_length - paf.target_end
            tail_length = paf.query_start if read_end == "left" else paf.query_length - paf.query_end
            if paf.mapq < thresholds["min_mapq"] or paf.overlap_bp < thresholds["min_overlap_bp"] or target_distance > thresholds["max_target_terminal_distance_bp"] or tail_length <= 0:
                continue
            tail = sequence[: paf.query_start] if read_end == "left" else sequence[paf.query_end :]
            if paf.strand == "-":
                tail = _reverse_complement(tail)
            if _max_motif_copies(tail, motif) < thresholds["min_motif_copies"]:
                continue
            grouped[(paf.target, target_end)].append(
                {
                    "target": paf.target,
                    "target_end": target_end,
                    "read_id": paf.query,
                    "read_end": read_end,
                    "strand": paf.strand,
                    "mapq": str(paf.mapq),
                    "overlap_bp": str(paf.overlap_bp),
                    "target_distance_bp": str(target_distance),
                    "extension_length": str(tail_length),
                    "query_start": str(paf.query_start),
                    "query_end": str(paf.query_end),
                    "tail": tail,
                }
            )
    rows: list[dict[str, str]] = []
    for candidates in grouped.values():
        candidates.sort(key=lambda row: (int(row["mapq"]), int(row["overlap_bp"])), reverse=True)
        representative = candidates[0]
        candidate_id = f"{representative['target']}:{representative['target_end']}"
        supporting = [row for row in candidates if _tails_compatible(representative["tail"], row["tail"])]
        support_ids = list(dict.fromkeys(row["read_id"] for row in supporting))
        accepted = len(support_ids) >= thresholds["min_supporting_reads"]
        for index, row in enumerate(candidates):
            decision = "rejected"
            reason = "insufficient_or_conflicting_read_support"
            if accepted and row is representative:
                decision = "auto_patch" if mode == "auto_patch" else "proposed"
                reason = "conservative_multi_read_support"
            elif accepted and row in supporting:
                reason = "supporting_read"
            rows.append({**{key: value for key, value in row.items() if key != "tail"}, "candidate_id": candidate_id, "supporting_reads": ",".join(support_ids), "decision": decision, "reason": reason})
    return rows


def _tails_compatible(first: str, second: str) -> bool:
    if not first or not second:
        return False
    if abs(len(first) - len(second)) > max(100, int(max(len(first), len(second)) * 0.2)):
        return False
    k = 11
    if min(len(first), len(second)) < k:
        return first == second
    first_kmers = {first[i : i + k] for i in range(len(first) - k + 1)}
    second_kmers = {second[i : i + k] for i in range(len(second) - k + 1)}
    return len(first_kmers & second_kmers) / min(len(first_kmers), len(second_kmers)) >= 0.2


def _iter_paf(path: Path) -> Iterator[PafRecord]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 12:
                continue
            yield PafRecord(
                query=fields[0], query_length=int(fields[1]), query_start=int(fields[2]), query_end=int(fields[3]), strand=fields[4],
                target=fields[5], target_length=int(fields[6]), target_start=int(fields[7]), target_end=int(fields[8]), mapq=int(fields[11]),
            )


def _write_patched_fasta(source: Path, destination: Path, patches: dict[tuple[str, str], str]) -> None:
    records = []
    for name, sequence in _iter_sequences(source):
        records.append((name, patches.get((name, "left"), "") + sequence + patches.get((name, "right"), "")))
    _write_fasta(destination, records)


def _write_fasta(path: Path, records: Iterable[tuple[str, str]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for name, sequence in records:
            _write_fasta_record(handle, name, sequence)


def _write_fasta_record(handle, name: str, sequence: str) -> None:
    handle.write(f">{name}\n")
    for offset in range(0, len(sequence), 80):
        handle.write(sequence[offset : offset + 80] + "\n")


def _write_tsv(path: Path, rows: Iterable[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _load_chromosome_list(path_value: str | None) -> set[str] | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists():
        return None
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")}


def _review_candidates(path: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in _read_tsv(path):
        if row.get("decision") != "proposed":
            continue
        candidates.append(
            {
                "candidate_id": row["candidate_id"],
                "target": row["target"],
                "target_end": row["target_end"],
                "read_id": row["read_id"],
                "supporting_reads": row["supporting_reads"],
                "extension_length": row["extension_length"],
                "reason": row["reason"],
            }
        )
    return candidates


def _materialize_t2t_patches(
    out: dict[str, Any],
    *,
    selected_candidate_ids: set[str] | None = None,
    allowed_decisions: set[str],
) -> dict[str, str] | None:
    candidates = _read_tsv(Path(out["telomere_extension_candidates_ref"]))
    selected = [
        row
        for row in candidates
        if row.get("decision") in allowed_decisions
        and (selected_candidate_ids is None or row.get("candidate_id") in selected_candidate_ids)
    ]
    if not selected:
        return None
    read_sequences = dict(_iter_sequences(Path(out["telomeric_reads_fasta_ref"])))
    patches: dict[tuple[str, str], str] = {}
    records: list[dict[str, str]] = []
    for row in selected:
        sequence = read_sequences.get(row["read_id"])
        if not sequence:
            continue
        tail = sequence[: int(row["query_start"])] if row["read_end"] == "left" else sequence[int(row["query_end"]) :]
        if row["strand"] == "-":
            tail = _reverse_complement(tail)
        if not tail:
            continue
        key = (row["target"], row["target_end"])
        if key in patches:
            continue
        patches[key] = tail
        records.append(
            {
                "candidate_id": row["candidate_id"],
                "target": row["target"],
                "target_end": row["target_end"],
                "source_read_id": row["read_id"],
                "source_strand": row["strand"],
                "extension_length": str(len(tail)),
                "supporting_reads": row["supporting_reads"],
                "mapq": row["mapq"],
                "overlap_bp": row["overlap_bp"],
            }
        )
    if not patches:
        return None
    patched_path = Path(out["out_dir"]) / "patched_assembly.fa"
    _write_patched_fasta(Path(out["input_assembly_ref"]), patched_path, patches)
    records_path = Path(out["out_dir"]) / "patch_records.tsv"
    _write_tsv(records_path, records, _PATCH_RECORD_FIELDS)
    return {"patched_assembly_ref": str(patched_path), "patch_records_ref": str(records_path)}


def _final_status(out: dict[str, Any], summary: list[dict[str, str]]) -> str:
    if not out.get("execution_enabled"):
        return "skipped"
    if out.get("status") == "skipped_no_long_reads":
        return "skipped_no_long_reads"
    if out.get("patched_assembly_ref"):
        return "completed"
    if out.get("mode") == "manual_review" and out.get("proposed_candidate_count", 0):
        return "blocked_human"
    if out.get("candidate_count", 0):
        return "completed_with_warnings"
    if any(row["status"] == "missing_signal" for row in summary) or out.get("notes"):
        return "completed_with_warnings"
    return "completed"


def _block_for_t2t_review(
    state: TopLevelState,
    out: dict[str, Any],
    *,
    report_path: Path,
    summary_path: Path,
    bundle_path: Path,
) -> dict:
    candidates = _review_candidates(Path(out["telomere_extension_candidates_ref"]))
    payload = ReviewPayload(
        stage="t2t",
        gate_id="t2t_review",
        reason="t2t_patch_candidates_require_human_selection",
        candidates=candidates,
        decisions_allowed=["accept_selected_candidates", "continue_without_patch", "stop_pipeline"],
        auto_decidable=False,
        report_refs={
            "t2t_finishing_report_ref": str(report_path),
            "telomere_status_summary_ref": str(summary_path),
            "telomere_extension_candidates_ref": out["telomere_extension_candidates_ref"],
            "bundle_ref": str(bundle_path),
        },
        notes=list(out.get("notes") or []),
    )
    request_ref = Path(out["out_dir"]) / "t2t_review_request.json"
    request_ref.write_text(json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        request_ref,
        stage="t2t",
        method_name=T2T_CONTRACT_METHOD,
        artifact_type="t2t.review_request",
        metadata={"auto_decidable": False},
    )
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    append_artifact_records(data_dir, session_id, [artifact])
    Ledger(data_dir).record_review_requested(
        session_id,
        "t2t",
        reason="t2t_patch_candidates_require_human_selection",
        candidates=candidates,
        review_request_artifact_ref=str(request_ref),
        auto_decidable=False,
    )
    outputs = dict(state.get("stage_outputs") or {})
    outputs["t2t"] = {
        **out,
        "status": "blocked_human",
        "review_request_artifact_ref": str(request_ref),
        "auto_decidable": False,
    }
    return {
        "current_stage": "t2t",
        "supervision_state": "blocked_human",
        "stage_outputs": outputs,
        "active_interrupt": {
            **payload.model_dump(mode="json"),
            "review_request_artifact_ref": str(request_ref),
            "blocked_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def _report_markdown(out: dict[str, Any], summary: list[dict[str, str]]) -> str:
    missing = sum(row["status"] == "missing_signal" for row in summary)
    lines = [
        "# T2T finishing report",
        "",
        f"- input assembly: `{out.get('input_assembly_ref', '')}`",
        f"- source stage: `{out.get('source_stage', '')}`",
        f"- ONT reads: `{out.get('ont_reads_ref') or 'not available'}`",
        f"- execution enabled: `{out.get('execution_enabled')}`",
        f"- telomere motif: `{out.get('telomere_motif')}`",
        f"- sequences with a missing terminal signal: `{missing}`",
        f"- telomeric reads: `{out.get('telomeric_read_count', 0)}`",
        f"- extension candidates: `{out.get('candidate_count', 0)}`",
        f"- selected patches: `{out.get('selected_patch_count', 0)}`",
        f"- annotation assembly: `{out.get('final_assembly_for_annotation', '')}`",
        "",
        "## Notes",
    ]
    lines.extend(f"- {note}" for note in (out.get("notes") or ["none"]))
    if out.get("skip_reason"):
        lines.append(f"- skip reason: {out['skip_reason']}")
    if out.get("patch_reason"):
        lines.append(f"- patch reason: {out['patch_reason']}")
    return "\n".join(lines) + "\n"


def _register_artifacts(state: TopLevelState, out: dict[str, Any], summary_path: Path, report_path: Path) -> list[Path]:
    candidates = [
        (out.get("assembly_telomere_scan_ref"), T2T_ASSEMBLY_TELOMERE_SCAN),
        (out.get("reads_telomere_scan_ref"), T2T_READS_TELOMERE_SCAN),
        (out.get("telomeric_reads_ref"), T2T_TELOMERIC_READS),
        (out.get("telomere_extension_candidates_ref"), T2T_EXTENSION_CANDIDATES),
        (str(summary_path), T2T_STATUS_SUMMARY),
        (str(report_path), T2T_FINISHING_REPORT),
        (out.get("patched_assembly_ref"), T2T_PATCHED_ASSEMBLY),
        (out.get("patch_records_ref"), T2T_PATCH_RECORDS),
    ]
    artifacts = []
    paths: list[Path] = []
    for value, artifact_type in candidates:
        if value and Path(value).exists():
            path = Path(value)
            paths.append(path)
            artifacts.append(ArtifactMeta.from_file(path, stage="t2t", method_name=T2T_CONTRACT_METHOD, artifact_type=artifact_type))
    if artifacts:
        append_artifact_records(absolute_data_dir(state), state["session_id"], artifacts)
    return paths


_ASSEMBLY_SCAN_FIELDS = ["sequence", "length", "left_motif_copies", "right_motif_copies", "left_has_signal", "right_has_signal"]
_READ_SCAN_FIELDS = ["read_id", "length", "left_motif_copies", "right_motif_copies", "left_has_signal", "right_has_signal"]
_CANDIDATE_FIELDS = ["candidate_id", "target", "target_end", "read_id", "read_end", "strand", "mapq", "overlap_bp", "target_distance_bp", "extension_length", "query_start", "query_end", "supporting_reads", "decision", "reason"]
_PATCH_RECORD_FIELDS = ["candidate_id", "target", "target_end", "source_read_id", "source_strand", "extension_length", "supporting_reads", "mapq", "overlap_bp"]
