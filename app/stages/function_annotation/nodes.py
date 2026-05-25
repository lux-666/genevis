"""Function annotation stage nodes for planning database-backed annotations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.graph.routing import next_stage_name
from app.reports.review_report import write_final_review_report
from app.schemas.artifacts import (
    ArtifactMeta,
    FunctionAnnotationBundle,
    FunctionAnnotationPlan,
    FunctionDatabaseProvenance,
)
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState

FUNCTION_CANDIDATE_METHODS = [
    "function_annotation.candidate.interproscan",
    "function_annotation.candidate.eggnog_mapper",
    "function_annotation.candidate.diamond",
    "function_annotation.candidate.kofamscan",
]
FUNCTION_CONTRACT_METHOD = "function_annotation.contract"


def _function_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("function_annotation", {}))


def _update_function_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["function_annotation"] = {**_function_output(state), **payload}
    return {"stage_outputs": outputs}


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_function_output(state, {"status": "failed", "reason": reason})


def enter_function_annotation(state: TopLevelState) -> dict:
    """Mark function annotation as running."""
    return {"supervision_state": "running_stage"}


def resolve_protein_inputs(state: TopLevelState) -> dict:
    """Resolve required predicted proteins and optional structural annotation refs."""
    gene_out = (state.get("stage_outputs") or {}).get("gene_annotation") or {}
    proteins_ref = _first_value(gene_out, ["proteins_fasta_ref", "predicted_proteins_ref", "predicted_proteins"])
    if not proteins_ref:
        return _fail_update(state, "missing proteins.fa for function_annotation")

    return _update_function_output(
        state,
        {
            "status": "resolved",
            "proteins_fasta_ref": proteins_ref,
            "genes_gff3_ref": _first_value(gene_out, ["genes_gff3_ref", "gene_models_ref", "gene_models"]),
            "transcripts_fasta_ref": _first_value(gene_out, ["transcripts_fasta_ref"]),
        },
    )


def plan_function_methods(state: TopLevelState) -> dict:
    """Plan function annotation candidates without running tools."""
    function_out = _function_output(state)
    if function_out.get("status") != "resolved":
        return {}

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    missing = [method for method in FUNCTION_CANDIDATE_METHODS if method not in registry.methods]
    if missing:
        return _fail_update(state, f"function_annotation descriptors missing: {missing}")

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    work_dir = data_dir / "work" / "function_annotation" / session_id
    expected_outputs = {
        "gene_function_tsv": str(work_dir / "gene_function.tsv"),
        "go_annotation_table": str(work_dir / "go_annotations.tsv"),
        "kegg_annotation_table": str(work_dir / "kegg_annotations.tsv"),
        "pfam_interpro_table": str(work_dir / "pfam_interpro.tsv"),
    }
    if function_out.get("genes_gff3_ref"):
        expected_outputs["annotated_gene_models_gff3"] = str(work_dir / "annotated_gene_models.gff3")

    return _update_function_output(
        state,
        {
            "status": "methods_planned",
            "candidate_methods": FUNCTION_CANDIDATE_METHODS,
            "method_selection_required": True,
            "expected_outputs": expected_outputs,
            "notes": [
                "execution_deferred_to_method_specific_task",
                "database_provenance_required_before_commit",
                "database_download_disabled_in_phase2",
            ],
        },
    )


def validate_database_provenance(state: TopLevelState) -> dict:
    """Validate database provenance and write the planning artifact."""
    function_out = _function_output(state)
    if function_out.get("status") != "methods_planned":
        return {}

    raw_provenance = function_out.get("database_provenance")
    provenance, error = _validate_provenance_records(raw_provenance, function_out["proteins_fasta_ref"])
    if error:
        return _fail_update(state, error)
    protein_error = _validate_protein_artifact_ref(function_out["proteins_fasta_ref"])
    if protein_error:
        return _fail_update(state, protein_error)

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "function_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = FunctionAnnotationPlan(
        session_id=session_id,
        status="planned",
        proteins_fasta_ref=function_out["proteins_fasta_ref"],
        genes_gff3_ref=function_out.get("genes_gff3_ref"),
        transcripts_fasta_ref=function_out.get("transcripts_fasta_ref"),
        candidate_methods=function_out.get("candidate_methods", []),
        expected_outputs=function_out.get("expected_outputs", {}),
        database_provenance=provenance,
        method_selection_required=True,
        notes=function_out.get("notes", []),
        metadata={
            "execution_enabled": False,
            "database_download_enabled": False,
            "provenance_required": True,
        },
    )
    plan_ref = out_dir / "function_annotation_plan.json"
    plan_ref.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        plan_ref,
        stage="function_annotation",
        method_name=FUNCTION_CONTRACT_METHOD,
        artifact_type="function_annotation.plan",
        metadata={
            "execution_enabled": False,
            "database_download_enabled": False,
            "provenance_required": True,
        },
    )
    append_artifact_records(data_dir, session_id, [artifact])
    Ledger(data_dir).record_artifact_registered(session_id, artifact.model_dump(mode="json"))
    return _update_function_output(
        state,
        {
            "status": "provenance_validated",
            "database_provenance": [record.model_dump(mode="json") for record in provenance],
            "plan_artifact_ref": str(plan_ref),
            "artifact_refs": [str(artifact.file_path)],
        },
    )


def commit_function_annotation(state: TopLevelState) -> dict:
    """Publish function annotation bundle and complete the workflow."""
    function_out = _function_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "function_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_outputs = function_out["expected_outputs"]
    provenance = [FunctionDatabaseProvenance.model_validate(record) for record in function_out.get("database_provenance", [])]
    bundle = FunctionAnnotationBundle(
        session_id=session_id,
        status="planned",
        proteins_fasta_ref=function_out["proteins_fasta_ref"],
        genes_gff3_ref=function_out.get("genes_gff3_ref"),
        transcripts_fasta_ref=function_out.get("transcripts_fasta_ref"),
        gene_function_tsv_ref=expected_outputs["gene_function_tsv"],
        go_annotation_table_ref=expected_outputs["go_annotation_table"],
        kegg_annotation_table_ref=expected_outputs["kegg_annotation_table"],
        pfam_interpro_table_ref=expected_outputs["pfam_interpro_table"],
        annotated_gene_models_gff3_ref=expected_outputs.get("annotated_gene_models_gff3"),
        candidate_methods=function_out.get("candidate_methods", []),
        database_provenance=provenance,
        plan_artifact_ref=function_out.get("plan_artifact_ref"),
        notes=function_out.get("notes", []),
        metadata={
            "execution_enabled": False,
            "database_download_enabled": False,
            "provenance_required": True,
        },
    )
    bundle_ref = out_dir / "function_annotation_bundle.json"
    bundle_ref.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        bundle_ref,
        stage="function_annotation",
        method_name=FUNCTION_CONTRACT_METHOD,
        artifact_type="function_annotation.bundle",
        metadata={
            "execution_enabled": False,
            "database_download_enabled": False,
            "provenance_required": True,
        },
    )
    append_artifact_records(data_dir, session_id, [artifact])
    Ledger(data_dir).record_artifact_registered(session_id, artifact.model_dump(mode="json"))
    final_report_ref = write_final_review_report(data_dir, session_id)

    outputs = dict(state.get("stage_outputs") or {})
    outputs["function_annotation"] = {
        **function_out,
        "status": "completed",
        "gene_function_tsv_ref": expected_outputs["gene_function_tsv"],
        "go_annotation_table_ref": expected_outputs["go_annotation_table"],
        "kegg_annotation_table_ref": expected_outputs["kegg_annotation_table"],
        "pfam_interpro_table_ref": expected_outputs["pfam_interpro_table"],
        "annotated_gene_models_gff3_ref": expected_outputs.get("annotated_gene_models_gff3"),
        "bundle_artifact_ref": str(bundle_ref),
        "final_review_report_artifact_ref": final_report_ref,
        "artifact_refs": [*function_out.get("artifact_refs", []), str(artifact.file_path), final_report_ref],
    }
    return {
        "stage_outputs": outputs,
        "current_stage": next_stage_name("function_annotation"),
        "supervision_state": "completed",
        "route_trace": [*state.get("route_trace", []), "function_annotation_stage"],
    }


def fail_function_annotation(state: TopLevelState) -> dict:
    """Stop workflow on invalid function annotation inputs or provenance."""
    function_out = _function_output(state)
    return {
        "current_stage": "function_annotation",
        "supervision_state": "failed_terminal",
        "last_error": function_out.get("reason", "function_annotation failed"),
    }


def _first_value(values: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = values.get(key)
        if value:
            return str(value)
    return None


def _validate_protein_artifact_ref(proteins_ref: str) -> str | None:
    if "://" in proteins_ref:
        return f"proteins_fasta_ref must be a local file path for function_annotation: {proteins_ref}"
    path = Path(proteins_ref)
    if not path.exists():
        return f"protein artifact not found for function_annotation: {proteins_ref}"
    if not path.is_file():
        return f"protein artifact is not a file for function_annotation: {proteins_ref}"
    return None


def _validate_provenance_records(
    raw_provenance: Any,
    proteins_ref: str,
) -> tuple[list[FunctionDatabaseProvenance], str | None]:
    if raw_provenance is None:
        return [], "missing database_provenance for function_annotation"
    if not isinstance(raw_provenance, list):
        return [], "database_provenance must be a non-empty list"
    if not raw_provenance:
        return [], "database_provenance must be a non-empty list"

    records: list[FunctionDatabaseProvenance] = []
    for index, raw_record in enumerate(raw_provenance):
        try:
            record = FunctionDatabaseProvenance.model_validate(raw_record)
        except ValidationError as exc:
            error = exc.errors()[0]
            field = ".".join(str(part) for part in error.get("loc", []))
            message = f"{field}: {error['msg']}" if field else error["msg"]
            return [], f"invalid database_provenance[{index}]: {message}"
        if record.input_proteins_ref != proteins_ref:
            return [], f"database_provenance[{index}].input_proteins_ref does not match proteins_fasta_ref"
        records.append(record)
    if not records:
        return [], "database_provenance must be a non-empty list"
    return records, None
