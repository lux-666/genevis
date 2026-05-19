"""Gene annotation stage nodes for planning structural annotation contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.execution.artifacts import append_artifact_records
from app.graph.routing import next_stage_name
from app.schemas.artifacts import ArtifactMeta, GeneAnnotationBundle, GeneAnnotationPlan
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState

GENE_CANDIDATE_METHODS = [
    "gene_annotation.candidate.braker3",
    "gene_annotation.candidate.maker",
    "gene_annotation.candidate.augustus",
    "gene_annotation.candidate.miniprot",
]
RNA_HELPER_METHODS = [
    "gene_annotation.helper.hisat2",
    "gene_annotation.helper.stringtie",
]
LONG_RNA_HELPER_METHODS = ["gene_annotation.helper.isoquant"]
PROTEIN_HELPER_METHODS = ["gene_annotation.helper.gemoma"]
GENE_CONTRACT_METHOD = "gene_annotation.contract"


def _gene_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("gene_annotation", {}))


def _update_gene_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["gene_annotation"] = {**_gene_output(state), **payload}
    return {"stage_outputs": outputs}


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_gene_output(state, {"status": "failed", "reason": reason})


def enter_gene_annotation(state: TopLevelState) -> dict:
    """Mark gene annotation as running."""
    return {"supervision_state": "running_stage"}


def resolve_gene_annotation_inputs(state: TopLevelState) -> dict:
    """Resolve masked genome and optional annotation evidence."""
    genome_ref = _softmasked_genome_ref(state)
    genome_source = "softmasked_genome"
    fallback_reason = None
    if not genome_ref:
        fallback_reason = _explicit_unmasked_fallback_reason(state)
        if not fallback_reason:
            return _fail_update(state, "missing softmasked_genome for gene_annotation")
        genome_ref = _final_assembly_for_annotation(state)
        genome_source = "final_assembly_for_annotation"
        if not genome_ref:
            return _fail_update(state, "missing final_assembly_for_annotation for gene_annotation fallback")

    rnaseq_refs = _asset_paths(state, {"clean_rnaseq_reads", "raw_rnaseq_reads"})
    long_rna_refs = _asset_paths(
        state,
        {"clean_isoseq_reads", "transcript_ccs_reads", "raw_isoseq_or_long_rna_reads"},
    )
    protein_refs = _asset_paths(state, {"reference_proteins"})
    repeat_ref = _repeat_annotation_ref(state)
    base_payload = {
        "genome_input_ref": genome_ref,
        "genome_input_source": genome_source,
        "unmasked_fallback_reason": fallback_reason,
        "repeat_annotation_ref": repeat_ref,
        "rnaseq_read_refs": rnaseq_refs,
        "long_rna_read_refs": long_rna_refs,
        "protein_evidence_refs": protein_refs,
        "lineage_or_species_hints": _lineage_or_species_hints(state),
    }
    provided = _provided_gene_annotation_refs(state)
    if any(provided.values()):
        missing = []
        if not provided.get("gene_models_ref"):
            missing.append("gene_models")
        if not provided.get("predicted_proteins_ref"):
            missing.append("predicted_proteins")
        if missing:
            return _fail_update(
                state,
                f"provided gene_annotation assets require {', '.join(missing)}",
            )
        review_triggers = _provided_annotation_review_triggers(
            {
                **base_payload,
                "transcripts_fasta_ref": provided.get("transcripts_fasta_ref"),
            }
        )
        return _update_gene_output(
            state,
            {
                **base_payload,
                "status": "provided_annotation_resolved",
                "gene_models_ref": provided["gene_models_ref"],
                "gene_models": provided["gene_models_ref"],
                "genes_gff3_ref": provided["gene_models_ref"],
                "predicted_proteins_ref": provided["predicted_proteins_ref"],
                "predicted_proteins": provided["predicted_proteins_ref"],
                "proteins_fasta_ref": provided["predicted_proteins_ref"],
                "transcripts_fasta_ref": provided.get("transcripts_fasta_ref"),
                "evidence_profile": "provided_annotation",
                "candidate_methods": [],
                "helper_methods": [],
                "method_selection_required": False,
                "review_required": bool(review_triggers),
                "review_triggers": review_triggers,
                "notes": [
                    "operator_provided_gene_annotation_adopted",
                    "gene_prediction_execution_not_requested",
                ],
            },
        )
    return _update_gene_output(state, {**base_payload, "status": "resolved"})


def select_evidence_profile(state: TopLevelState) -> dict:
    """Select the evidence profile from available annotation evidence."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "resolved":
        return {}

    available = []
    if gene_out.get("rnaseq_read_refs"):
        available.append("rnaseq")
    if gene_out.get("long_rna_read_refs"):
        available.append("long_rna")
    if gene_out.get("protein_evidence_refs"):
        available.append("protein")

    if not available:
        profile = "no_evidence"
    elif len(available) > 1:
        profile = "mixed_evidence"
    else:
        profile = available[0]

    return _update_gene_output(
        state,
        {
            "status": "evidence_profile_selected",
            "evidence_profile": profile,
            "available_evidence": available,
        },
    )


def plan_gene_methods(state: TopLevelState) -> dict:
    """Plan candidate gene annotation methods without selecting a default."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "evidence_profile_selected":
        return {}

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    profile = gene_out["evidence_profile"]
    candidate_methods = _candidate_methods(profile)
    helper_methods = _helper_methods(gene_out.get("available_evidence", []))
    required_methods = [*candidate_methods, *helper_methods]
    missing = [method for method in required_methods if method not in registry.methods]
    if missing:
        return _fail_update(state, f"gene_annotation descriptors missing: {missing}")

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    work_dir = data_dir / "work" / "gene_annotation" / session_id
    expected_outputs = {
        "genes_gff3": str(work_dir / "genes.gff3"),
        "transcripts_fasta": str(work_dir / "transcripts.fa"),
        "predicted_proteins": str(work_dir / "proteins.fa"),
        "evidence_report": str(work_dir / "evidence_report.json"),
    }
    notes = ["execution_deferred_to_method_specific_task"]
    if profile == "no_evidence":
        notes.append("ab_initio_planning_explicit_no_evidence")
    return _update_gene_output(
        state,
        {
            "status": "methods_planned",
            "candidate_methods": candidate_methods,
            "helper_methods": helper_methods,
            "method_selection_required": True,
            "expected_outputs": expected_outputs,
            "notes": notes,
        },
    )


def plan_evidence_merge_or_review(state: TopLevelState) -> dict:
    """Write a gene annotation plan and record review triggers."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "methods_planned":
        return {}

    review_triggers = _review_triggers(gene_out)
    review_required = bool(review_triggers)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "gene_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = GeneAnnotationPlan(
        session_id=session_id,
        status="planned",
        genome_input_ref=gene_out["genome_input_ref"],
        genome_input_source=gene_out["genome_input_source"],
        unmasked_fallback_reason=gene_out.get("unmasked_fallback_reason"),
        repeat_annotation_ref=gene_out.get("repeat_annotation_ref"),
        rnaseq_read_refs=gene_out.get("rnaseq_read_refs", []),
        long_rna_read_refs=gene_out.get("long_rna_read_refs", []),
        protein_evidence_refs=gene_out.get("protein_evidence_refs", []),
        lineage_or_species_hints=gene_out.get("lineage_or_species_hints", {}),
        evidence_profile=gene_out["evidence_profile"],
        candidate_methods=gene_out.get("candidate_methods", []),
        helper_methods=gene_out.get("helper_methods", []),
        expected_outputs=gene_out.get("expected_outputs", {}),
        review_required=review_required,
        review_triggers=review_triggers,
        method_selection_required=True,
        notes=gene_out.get("notes", []),
        metadata={"execution_enabled": False},
    )
    plan_ref = out_dir / "gene_annotation_plan.json"
    plan_ref.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        plan_ref,
        stage="gene_annotation",
        method_name=GENE_CONTRACT_METHOD,
        artifact_type="gene_annotation.plan",
        metadata={
            "evidence_profile": gene_out["evidence_profile"],
            "genome_input_source": gene_out["genome_input_source"],
            "execution_enabled": False,
            "review_required": review_required,
        },
    )
    append_artifact_records(data_dir, session_id, [artifact])
    return _update_gene_output(
        state,
        {
            "status": "review_planned",
            "review_required": review_required,
            "review_triggers": review_triggers,
            "plan_artifact_ref": str(plan_ref),
            "artifact_refs": [str(artifact.file_path)],
        },
    )


def commit_gene_annotation(state: TopLevelState) -> dict:
    """Publish the gene annotation bundle and advance to function annotation."""
    gene_out = _gene_output(state)
    if gene_out.get("status") == "provided_annotation_resolved":
        return _commit_adopted_gene_annotation(state)
    return _commit_planned_gene_annotation(state)


def _commit_planned_gene_annotation(state: TopLevelState) -> dict:
    gene_out = _gene_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "gene_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_outputs = gene_out["expected_outputs"]
    bundle = GeneAnnotationBundle(
        session_id=session_id,
        status="planned",
        genome_input_ref=gene_out["genome_input_ref"],
        genome_input_source=gene_out["genome_input_source"],
        genes_gff3_ref=expected_outputs["genes_gff3"],
        transcripts_fasta_ref=expected_outputs["transcripts_fasta"],
        predicted_proteins_ref=expected_outputs["predicted_proteins"],
        evidence_report_ref=expected_outputs["evidence_report"],
        repeat_annotation_ref=gene_out.get("repeat_annotation_ref"),
        evidence_profile=gene_out["evidence_profile"],
        candidate_methods=gene_out.get("candidate_methods", []),
        helper_methods=gene_out.get("helper_methods", []),
        review_required=gene_out.get("review_required", False),
        review_triggers=gene_out.get("review_triggers", []),
        plan_artifact_ref=gene_out.get("plan_artifact_ref"),
        notes=gene_out.get("notes", []),
        metadata={"execution_enabled": False},
    )
    bundle_ref = out_dir / "gene_annotation_bundle.json"
    bundle_ref.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        bundle_ref,
        stage="gene_annotation",
        method_name=GENE_CONTRACT_METHOD,
        artifact_type="gene_annotation.bundle",
        metadata={
            "evidence_profile": gene_out["evidence_profile"],
            "genome_input_source": gene_out["genome_input_source"],
            "execution_enabled": False,
            "review_required": gene_out.get("review_required", False),
        },
    )
    append_artifact_records(data_dir, session_id, [artifact])

    outputs = dict(state.get("stage_outputs") or {})
    outputs["gene_annotation"] = {
        **gene_out,
        "status": "completed",
        "gene_models_ref": expected_outputs["genes_gff3"],
        "gene_models": expected_outputs["genes_gff3"],
        "genes_gff3_ref": expected_outputs["genes_gff3"],
        "transcripts_fasta_ref": expected_outputs["transcripts_fasta"],
        "predicted_proteins_ref": expected_outputs["predicted_proteins"],
        "predicted_proteins": expected_outputs["predicted_proteins"],
        "proteins_fasta_ref": expected_outputs["predicted_proteins"],
        "evidence_report_ref": expected_outputs["evidence_report"],
        "bundle_artifact_ref": str(bundle_ref),
        "artifact_refs": [*gene_out.get("artifact_refs", []), str(artifact.file_path)],
    }
    trace = [*state.get("route_trace", []), "gene_annotation_stage"]
    return {
        "stage_outputs": outputs,
        "current_stage": next_stage_name("gene_annotation"),
        "supervision_state": "running_stage",
        "route_trace": trace,
    }


def _commit_adopted_gene_annotation(state: TopLevelState) -> dict:
    gene_out = _gene_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "gene_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    review_triggers = list(gene_out.get("review_triggers") or [])
    bundle = GeneAnnotationBundle(
        session_id=session_id,
        status="adopted_existing_annotation",
        genome_input_ref=gene_out["genome_input_ref"],
        genome_input_source=gene_out["genome_input_source"],
        genes_gff3_ref=gene_out["genes_gff3_ref"],
        transcripts_fasta_ref=gene_out.get("transcripts_fasta_ref"),
        predicted_proteins_ref=gene_out["predicted_proteins_ref"],
        evidence_report_ref=None,
        repeat_annotation_ref=gene_out.get("repeat_annotation_ref"),
        evidence_profile="provided_annotation",
        candidate_methods=[],
        helper_methods=[],
        review_required=bool(review_triggers),
        review_triggers=review_triggers,
        plan_artifact_ref=None,
        notes=gene_out.get("notes", []),
        metadata={
            "execution_enabled": False,
            "adoption_mode": "provided_gene_annotation",
            "source": "manifest",
        },
    )
    bundle_ref = out_dir / "gene_annotation_bundle.json"
    bundle_ref.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifacts = [
        ArtifactMeta.from_file(
            Path(gene_out["genes_gff3_ref"]),
            stage="gene_annotation",
            method_name=GENE_CONTRACT_METHOD,
            artifact_type="gene_annotation.provided_gene_models",
            metadata={"adoption_mode": "provided_gene_annotation"},
        ),
        ArtifactMeta.from_file(
            Path(gene_out["predicted_proteins_ref"]),
            stage="gene_annotation",
            method_name=GENE_CONTRACT_METHOD,
            artifact_type="gene_annotation.provided_predicted_proteins",
            metadata={"adoption_mode": "provided_gene_annotation"},
        ),
    ]
    if gene_out.get("transcripts_fasta_ref"):
        artifacts.append(
            ArtifactMeta.from_file(
                Path(gene_out["transcripts_fasta_ref"]),
                stage="gene_annotation",
                method_name=GENE_CONTRACT_METHOD,
                artifact_type="gene_annotation.provided_transcripts_fasta",
                metadata={"adoption_mode": "provided_gene_annotation"},
            )
        )
    bundle_artifact = ArtifactMeta.from_file(
        bundle_ref,
        stage="gene_annotation",
        method_name=GENE_CONTRACT_METHOD,
        artifact_type="gene_annotation.bundle",
        metadata={
            "evidence_profile": "provided_annotation",
            "genome_input_source": gene_out["genome_input_source"],
            "execution_enabled": False,
            "adoption_mode": "provided_gene_annotation",
            "review_required": bool(review_triggers),
        },
    )
    artifacts.append(bundle_artifact)
    append_artifact_records(data_dir, session_id, artifacts)

    outputs = dict(state.get("stage_outputs") or {})
    outputs["gene_annotation"] = {
        **gene_out,
        "status": "completed",
        "bundle_status": "adopted_existing_annotation",
        "method_selection_required": False,
        "evidence_report_ref": None,
        "bundle_artifact_ref": str(bundle_ref),
        "artifact_refs": [str(artifact.file_path) for artifact in artifacts],
    }
    trace = [*state.get("route_trace", []), "gene_annotation_stage"]
    return {
        "stage_outputs": outputs,
        "current_stage": next_stage_name("gene_annotation"),
        "supervision_state": "running_stage",
        "route_trace": trace,
    }


def fail_gene_annotation(state: TopLevelState) -> dict:
    """Stop workflow on invalid gene annotation inputs or descriptors."""
    gene_out = _gene_output(state)
    return {
        "current_stage": "gene_annotation",
        "supervision_state": "failed_terminal",
        "last_error": gene_out.get("reason", "gene_annotation failed"),
    }


def _softmasked_genome_ref(state: TopLevelState) -> str | None:
    repeat_out = (state.get("stage_outputs") or {}).get("repeat_annotation") or {}
    for key in ["softmasked_genome_ref", "softmasked_genome"]:
        value = repeat_out.get(key)
        if value:
            return str(value)
    return None


def _repeat_annotation_ref(state: TopLevelState) -> str | None:
    repeat_out = (state.get("stage_outputs") or {}).get("repeat_annotation") or {}
    for key in ["repeat_annotation_gff3_ref", "repeat_annotation"]:
        value = repeat_out.get(key)
        if value:
            return str(value)
    return None


def _explicit_unmasked_fallback_reason(state: TopLevelState) -> str | None:
    gene_out = _gene_output(state)
    if gene_out.get("unmasked_fallback_reason"):
        return str(gene_out["unmasked_fallback_reason"])
    repeat_out = (state.get("stage_outputs") or {}).get("repeat_annotation") or {}
    if repeat_out.get("unmasked_fallback_reason"):
        return str(repeat_out["unmasked_fallback_reason"])
    skip_reason = (state.get("skip_reasons") or {}).get("repeat_annotation")
    return str(skip_reason) if skip_reason else None


def _final_assembly_for_annotation(state: TopLevelState) -> str | None:
    outputs = state.get("stage_outputs") or {}
    hic_out = outputs.get("hic_scaffolding") or {}
    value = hic_out.get("final_assembly_for_annotation")
    if value:
        return str(value)
    review_out = outputs.get("assembly_review") or {}
    for key in ["final_assembly_for_scaffolding_or_annotation", "final_assembly_ref"]:
        value = review_out.get(key)
        if value:
            return str(value)
    return None


def _registry_assets(state: TopLevelState, asset_types: set[str]) -> list[dict[str, Any]]:
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return []
    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return []
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    return [
        asset
        for asset in registry.get("assets", [])
        if asset.get("asset_type") in asset_types and asset.get("path")
    ]


def _asset_paths(state: TopLevelState, asset_types: set[str]) -> list[str]:
    return [str(asset.get("path")) for asset in _registry_assets(state, asset_types)]


def _first_asset_path(state: TopLevelState, asset_type: str) -> str | None:
    assets = _registry_assets(state, {asset_type})
    if len(assets) > 1:
        raise ValueError(f"adoption asset {asset_type!r} has {len(assets)} registry entries; manifest validation should have rejected this")
    return str(assets[0]["path"]) if assets else None


def _provided_gene_annotation_refs(state: TopLevelState) -> dict[str, str | None]:
    return {
        "gene_models_ref": _first_asset_path(state, "gene_models"),
        "predicted_proteins_ref": _first_asset_path(state, "predicted_proteins"),
        "transcripts_fasta_ref": _first_asset_path(state, "transcripts_fasta"),
    }


def _provided_annotation_review_triggers(gene_out: dict[str, Any]) -> list[str]:
    triggers: list[str] = []
    if gene_out.get("genome_input_source") == "final_assembly_for_annotation":
        triggers.append("unmasked_genome_fallback_requires_review")
    if gene_out.get("rnaseq_read_refs") or gene_out.get("long_rna_read_refs") or gene_out.get("protein_evidence_refs"):
        triggers.append("provided_annotation_with_additional_evidence_review")
    return triggers


def _lineage_or_species_hints(state: TopLevelState) -> dict[str, Any]:
    gene_out = _gene_output(state)
    hints = dict(gene_out.get("lineage_or_species_hints") or {})
    inventory_out = (state.get("stage_outputs") or {}).get("inventory") or {}
    for key in ["species", "lineage", "busco_lineage"]:
        if inventory_out.get(key) and key not in hints:
            hints[key] = inventory_out[key]
    return hints


def _candidate_methods(profile: str) -> list[str]:
    if profile == "no_evidence":
        return ["gene_annotation.candidate.augustus"]
    if profile == "protein":
        return [
            "gene_annotation.candidate.braker3",
            "gene_annotation.candidate.maker",
            "gene_annotation.candidate.miniprot",
        ]
    if profile in {"rnaseq", "long_rna"}:
        return [
            "gene_annotation.candidate.braker3",
            "gene_annotation.candidate.maker",
            "gene_annotation.candidate.augustus",
        ]
    return GENE_CANDIDATE_METHODS


def _helper_methods(available_evidence: list[str]) -> list[str]:
    helpers: list[str] = []
    if "rnaseq" in available_evidence:
        helpers.extend(RNA_HELPER_METHODS)
    if "long_rna" in available_evidence:
        helpers.extend(LONG_RNA_HELPER_METHODS)
    if "protein" in available_evidence:
        helpers.extend(PROTEIN_HELPER_METHODS)
    return helpers


def _review_triggers(gene_out: dict[str, Any]) -> list[str]:
    triggers: list[str] = []
    if gene_out.get("evidence_profile") == "mixed_evidence":
        triggers.append("evidence_merge_policy_required")
    available = set(gene_out.get("available_evidence", []))
    if "protein" in available and ({"rnaseq", "long_rna"} & available):
        triggers.append("rna_protein_evidence_conflict_review")
    if gene_out.get("genome_input_source") == "final_assembly_for_annotation":
        triggers.append("unmasked_genome_fallback_requires_review")
    if gene_out.get("evidence_profile") == "no_evidence":
        triggers.append("species_or_training_policy_required")
    return triggers
