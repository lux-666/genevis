"""Function annotation stage nodes for planning database-backed annotations."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.graph.routing import next_stage_name
from app.reports.review_report import write_final_review_report
from app.schemas.artifacts import (
    ArtifactMeta,
    FunctionAnnotationBundle,
    FunctionAnnotationPlan,
    FunctionDatabaseProvenance,
)
from app.schemas.descriptors import DescriptorRegistry, FrozenAttemptSpec
from app.schemas.state import TopLevelState

FUNCTION_CANDIDATE_METHODS = [
    "function_annotation.candidate.interproscan",
    "function_annotation.candidate.eggnog_mapper",
    "function_annotation.candidate.diamond",
    "function_annotation.candidate.kofamscan",
]
FUNCTION_CONTRACT_METHOD = "function_annotation.contract"

DIAMOND_METHOD = "function_annotation.candidate.diamond"

FUNCTION_EXECUTABLE_METHODS: dict[str, dict[str, str]] = {
    "function_annotation.candidate.diamond": {"suffix": "diamond", "frozen_status": "diamond_frozen", "ran_status": "diamond_ran"},
}


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
    if _function_output(state).get("status"):
        return {}
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


# --- shared function method execution helpers (freeze -> run -> collect) -------


def _freeze_func_method_attempt(
    state: TopLevelState,
    *,
    method_name: str,
    cfg: dict[str, str],
    script_body: str,
    extra_metadata: dict[str, Any] | None = None,
) -> dict:
    function_out = _function_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]

    env_spec, blocked = resolve_method_env(
        function_out,
        stage="function_annotation",
        method_name=method_name,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_function_output(state, blocked)

    working_dir = data_dir / "work" / "function_annotation" / session_id / cfg["suffix"]
    artifact_dir = data_dir / "artifacts" / "function_annotation" / session_id / cfg["suffix"]
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    output_tsv = artifact_dir / f"{cfg['suffix']}_hits.tsv"

    script_path = working_dir / f"run_{cfg['suffix']}.sh"
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:function_annotation:{cfg['suffix']}",
        session_id=session_id,
        stage="function_annotation",
        method_name=method_name,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[output_tsv],
        env=function_out.get("env"),
        env_spec=env_spec,
        metadata={
            "proteins_fasta_ref": str(function_out["proteins_fasta_ref"]),
            "output_tsv": str(output_tsv),
            **(extra_metadata or {}),
        },
    )

    return _update_function_output(
        state,
        {
            "status": cfg["frozen_status"],
            f"{cfg['suffix']}_frozen_attempt": attempt.model_dump(mode="json"),
        },
    )


def _run_func_method_attempt(state: TopLevelState, *, cfg: dict[str, str]) -> dict:
    function_out = _function_output(state)
    attempt = FrozenAttemptSpec.model_validate(function_out[f"{cfg['suffix']}_frozen_attempt"])
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label=cfg["suffix"],
    )
    if not outcome.success:
        return _update_function_output(
            state,
            {
                "status": "failed",
                "reason": outcome.error,
                f"{cfg['suffix']}_execution_result": outcome.result.model_dump(mode="json"),
            },
        )
    return _update_function_output(
        state,
        {
            "status": cfg["ran_status"],
            f"{cfg['suffix']}_execution_result": outcome.result.model_dump(mode="json"),
        },
    )


def _collect_func_method_artifacts(
    state: TopLevelState, *, method_name: str, cfg: dict[str, str], artifact_type: str
) -> dict:
    function_out = _function_output(state)
    attempt = FrozenAttemptSpec.model_validate(function_out[f"{cfg['suffix']}_frozen_attempt"])
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    output_tsv = Path(str(attempt.metadata["output_tsv"]))

    if not output_tsv.exists():
        reason = f"expected {cfg['suffix']} TSV missing: {output_tsv}"
        Ledger(data_dir).record_attempt_failed(
            attempt.session_id, attempt, return_code=0, error=reason,
        )
        return _update_function_output(state, {"status": "failed", "reason": reason})

    artifact = ArtifactMeta.from_file(
        output_tsv,
        stage="function_annotation",
        method_name=method_name,
        artifact_type=artifact_type,
        metadata={"attempt_id": attempt.attempt_id},
    )
    append_artifact_records(data_dir, session_id, [artifact])

    Ledger(data_dir).record_attempt_succeeded(
        attempt.session_id, attempt,
        artifacts=[{"path": str(output_tsv), "artifact_type": artifact_type}],
    )

    return _update_function_output(
        state,
        {
            "status": f"{cfg['suffix']}_completed",
            f"{cfg['suffix']}_hits_ref": str(output_tsv),
            "artifact_refs": [*function_out.get("artifact_refs", []), str(artifact.file_path)],
        },
    )


# --- diamond method execution nodes ----------------------------------------------


def freeze_diamond_attempt(state: TopLevelState) -> dict:
    function_out = _function_output(state)
    if function_out.get("status") != "provenance_validated":
        return {}

    db_provenance = function_out.get("database_provenance") or []
    db_path = None
    for record in db_provenance:
        if record.get("method_name") == DIAMOND_METHOD:
            db_path = record.get("database_source_ref")
            break
    if not db_path:
        return _update_function_output(state, {"status": "diamond_completed"})

    proteins_ref = function_out.get("proteins_fasta_ref")
    if not proteins_ref:
        return _fail_update(state, "diamond requires proteins_fasta_ref")

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    working_dir = data_dir / "work" / "function_annotation" / session_id / "diamond"
    dmnd_index = working_dir / "reference.dmnd"

    cfg = FUNCTION_EXECUTABLE_METHODS[DIAMOND_METHOD]
    artifact_dir = data_dir / "artifacts" / "function_annotation" / session_id / cfg["suffix"]
    output_tsv = artifact_dir / f"{cfg['suffix']}_hits.tsv"

    threads = function_out.get("diamond_parameters", {}).get("threads", 4)
    try:
        threads = int(threads)
    except (TypeError, ValueError):
        threads = 4

    script = (
        "set -e\n"
        f"diamond makedb --in {shlex.quote(str(db_path))} -d {shlex.quote(str(dmnd_index))}\n"
        f"diamond blastp --db {shlex.quote(str(dmnd_index))} --query {shlex.quote(str(proteins_ref))}"
        f" --outfmt 6 --threads {threads} --out {shlex.quote(str(output_tsv))}\n"
        f"test -f {shlex.quote(str(output_tsv))}\n"
    )

    return _freeze_func_method_attempt(
        state,
        method_name=DIAMOND_METHOD,
        cfg=cfg,
        script_body=script,
        extra_metadata={"database_source_ref": db_path, "dmnd_index": str(dmnd_index), "threads": threads},
    )


def run_diamond_attempt(state: TopLevelState) -> dict:
    function_out = _function_output(state)
    if function_out.get("status") != "diamond_frozen":
        return {}
    return _run_func_method_attempt(state, cfg=FUNCTION_EXECUTABLE_METHODS[DIAMOND_METHOD])


def collect_diamond_artifacts(state: TopLevelState) -> dict:
    function_out = _function_output(state)
    if function_out.get("status") != "diamond_ran":
        return {}
    return _collect_func_method_artifacts(
        state,
        method_name=DIAMOND_METHOD,
        cfg=FUNCTION_EXECUTABLE_METHODS[DIAMOND_METHOD],
        artifact_type="function_annotation.diamond_hits",
    )


def commit_function_annotation(state: TopLevelState) -> dict:
    """Publish function annotation bundle and complete the workflow."""
    function_out = _function_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "function_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_outputs = function_out.get("expected_outputs", {})
    diamond_hits_ref = function_out.get("diamond_hits_ref")
    materialized_gene_function = _existing_file_ref(expected_outputs.get("gene_function_tsv"))
    materialized_go = _existing_file_ref(expected_outputs.get("go_annotation_table"))
    materialized_kegg = _existing_file_ref(expected_outputs.get("kegg_annotation_table"))
    materialized_pfam = _existing_file_ref(expected_outputs.get("pfam_interpro_table"))
    materialized_annotated_gff = _existing_file_ref(expected_outputs.get("annotated_gene_models_gff3"))
    provenance = [FunctionDatabaseProvenance.model_validate(record) for record in function_out.get("database_provenance", [])]
    bundle = FunctionAnnotationBundle(
        session_id=session_id,
        status="planned",
        proteins_fasta_ref=function_out["proteins_fasta_ref"],
        genes_gff3_ref=function_out.get("genes_gff3_ref"),
        transcripts_fasta_ref=function_out.get("transcripts_fasta_ref"),
        diamond_hits_ref=diamond_hits_ref,
        gene_function_tsv_ref=materialized_gene_function,
        go_annotation_table_ref=materialized_go,
        kegg_annotation_table_ref=materialized_kegg,
        pfam_interpro_table_ref=materialized_pfam,
        annotated_gene_models_gff3_ref=materialized_annotated_gff,
        candidate_methods=function_out.get("candidate_methods", []),
        database_provenance=provenance,
        plan_artifact_ref=function_out.get("plan_artifact_ref"),
        notes=function_out.get("notes", []),
        metadata={
            "execution_enabled": bool(diamond_hits_ref),
            "database_download_enabled": False,
            "provenance_required": True,
            "materialized_outputs": {
                "diamond_hits": bool(diamond_hits_ref),
                "gene_function_tsv": bool(materialized_gene_function),
                "go_annotation_table": bool(materialized_go),
                "kegg_annotation_table": bool(materialized_kegg),
                "pfam_interpro_table": bool(materialized_pfam),
                "annotated_gene_models_gff3": bool(materialized_annotated_gff),
            },
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
            "execution_enabled": bool(diamond_hits_ref),
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
        "diamond_hits_ref": diamond_hits_ref,
        "gene_function_tsv_ref": materialized_gene_function,
        "go_annotation_table_ref": materialized_go,
        "kegg_annotation_table_ref": materialized_kegg,
        "pfam_interpro_table_ref": materialized_pfam,
        "annotated_gene_models_gff3_ref": materialized_annotated_gff,
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


def _existing_file_ref(ref: Any) -> str | None:
    if not ref:
        return None
    path = Path(str(ref))
    return str(path) if path.is_file() else None


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
        if record.input_proteins_ref is None:
            record = record.model_copy(update={"input_proteins_ref": proteins_ref})
        elif record.input_proteins_ref != proteins_ref:
            return [], f"database_provenance[{index}].input_proteins_ref does not match proteins_fasta_ref"
        records.append(record)
    if not records:
        return [], "database_provenance must be a non-empty list"
    return records, None
