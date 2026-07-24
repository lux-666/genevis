"""Inventory stage nodes: manifest loading, validation, and asset registration.

Inventory performs no external side effects other than writing the asset
registry artifact and ledger events.  It does not run bioinformatics tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from app.review.persistence import persist_review_decision
from app.schemas.artifacts import ArtifactMeta
from app.schemas.assets import AssetRecord
from app.schemas.manifest import InputManifest, validate_manifest
from app.schemas.resource_ref import ResourceRef
from app.schemas.shared_parameters import SharedParameters
from app.schemas.state import TopLevelState
from app.stages.base import append_route_trace, resolve_run_ref
from app.storage.artifacts import publish_artifact_record
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths


def _load_manifest(manifest_ref: str) -> InputManifest:
    """Load the canonical runtime manifest.

    Entry materialization owns path normalization. Runtime consumers reject
    relative paths instead of guessing whether they belong to the source
    manifest directory, the run control directory, or the current process.
    """
    path = Path(manifest_ref)
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix == ".json":
            raw = json.load(handle)
        else:
            raw = yaml.safe_load(handle) or {}
    manifest = InputManifest.model_validate(raw)
    relative_paths = [
        declared
        for asset in manifest.assets
        for declared in asset.paths
        if not Path(declared).is_absolute()
    ]
    if relative_paths:
        raise ValueError(
            "runtime manifest contains non-canonical relative asset paths: "
            f"{relative_paths}"
        )
    return manifest


def _build_asset_records(
    manifest: InputManifest,
    inferred: dict[str, Any],
    *,
    manifest_ref: str,
) -> list[AssetRecord]:
    """Convert validated manifest specs into AssetRecords."""
    records: list[AssetRecord] = []
    for spec in manifest.assets:
        props = inferred.get(spec.asset_type, {})
        for p in spec.paths:
            path = Path(p)
            records.append(
                AssetRecord(
                    asset_id=f"{manifest.session_id}:{spec.asset_type}:{path.name}",
                    asset_type=spec.asset_type,
                    path=path,
                    sample_id=spec.sample_id or manifest.sample_id,
                    platform=props.get("inferred_platform", spec.platform),
                    format=props.get("inferred_format", spec.format),
                    checksum_status="pending",
                    allowed_stages=[],
                    metadata={
                        "layout": props.get("inferred_layout", spec.layout),
                        "compression": props.get(
                            "inferred_compression", spec.compression
                        ),
                        **spec.metadata,
                    },
                    provenance={
                        "manifest_ref": manifest_ref,
                        "declared_path": p,
                    },
                )
            )
    return records


def _entry_mode(manifest: InputManifest) -> str:
    """Return the entry mode implied by manifest assets."""
    asset_types = {asset.asset_type for asset in manifest.assets}
    supporting = {
        "repeat_library",
        "gene_models",
        "predicted_proteins",
        "transcripts_fasta",
    }
    non_supporting = asset_types - supporting
    if non_supporting == {"assembly_fasta_registered"}:
        return "existing_assembly"
    return "raw_inputs"


def _write_registry(
    records: list[AssetRecord],
    *,
    data_dir: Path,
    session_id: str,
) -> Path:
    """Publish the asset registry through the shared immutable boundary."""
    paths = SessionPaths.from_run_dir(data_dir)
    destination = paths.stage_artifacts_dir("asset_registry") / "registry.json"
    source = paths.work_path("inventory", "registry.json")
    source.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "assets": [r.model_dump(mode="json") for r in records],
    }
    source.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        source,
        stage="inventory",
        method_name="inventory.register_assets",
        artifact_type="asset_registry",
    )
    return publish_artifact_record(
        paths.run_dir,
        session_id,
        artifact,
        destination=destination,
    ).path


def enter_inventory(state: TopLevelState) -> dict:
    """Mark inventory as running.  No side effects."""
    return {"supervision_state": "running_stage"}


def validate_inputs(state: TopLevelState) -> dict:
    """Load and validate the input manifest.

    Returns state updates that the routing node will use to decide
    success, failure, or review-required.
    """
    manifest_ref = state.get("input_manifest_ref")
    if not manifest_ref:
        return {
            "stage_outputs": {
                **(state.get("stage_outputs") or {}),
                "inventory": {
                    "status": "failed",
                    "reason": "missing input_manifest_ref",
                },
            },
        }

    try:
        manifest = _load_manifest(str(resolve_run_ref(state, manifest_ref)))
        canonical_shared = _canonical_shared_parameters(state, manifest)
    except Exception as exc:
        return {
            "stage_outputs": {
                **(state.get("stage_outputs") or {}),
                "inventory": {
                    "status": "failed",
                    "reason": f"manifest load error: {exc}",
                },
            },
        }

    _apply_shared_platform_override(manifest, state)
    report = validate_manifest(manifest)
    update = {
        "stage_outputs": {
            **(state.get("stage_outputs") or {}),
            "inventory": {
                "status": "validated",
                "valid": report["valid"],
                "errors": report["errors"],
                "warnings": report["warnings"],
                "asset_count": len(manifest.assets),
                "entry_mode": _entry_mode(manifest),
                "genome_size_estimate": manifest.genome_size_estimate,
                "target_assembly_level": manifest.target_assembly_level,
            },
        },
    }
    if canonical_shared is not None:
        update["shared_parameters"] = canonical_shared
    return update


def _canonical_shared_parameters(
    state: TopLevelState,
    manifest: InputManifest,
) -> dict[str, Any] | None:
    """Project the manifest-owned genome-size prior into runtime shared facts."""

    manifest_size = manifest.genome_size_estimate
    if manifest_size is None:
        return None

    raw_shared = state.get("shared_parameters")
    if hasattr(raw_shared, "model_dump"):
        raw_shared = raw_shared.model_dump(exclude_none=True)
    if raw_shared is None:
        shared: dict[str, Any] = {}
    elif isinstance(raw_shared, dict):
        shared = dict(raw_shared)
    else:
        raise ValueError("shared_parameters must be a mapping")

    existing_size = shared.get("estimated_genome_size")
    if existing_size is not None and existing_size != manifest_size:
        raise ValueError(
            "runtime shared_parameters.estimated_genome_size conflicts with "
            "manifest.genome_size_estimate"
        )
    shared["estimated_genome_size"] = manifest_size
    return SharedParameters.model_validate(shared).model_dump(exclude_none=True)


def register_assets(state: TopLevelState) -> dict:
    """Register assets, write registry artifact, and record ledger events."""
    inventory_out = (state.get("stage_outputs") or {}).get("inventory", {})
    if inventory_out.get("status") != "validated" or not inventory_out.get("valid"):
        return {}

    manifest_ref = state["input_manifest_ref"]
    manifest = _load_manifest(str(resolve_run_ref(state, manifest_ref)))
    _apply_shared_platform_override(manifest, state)
    report = validate_manifest(manifest)
    inferred = report["inferred"]

    records = _build_asset_records(manifest, inferred, manifest_ref=manifest_ref)
    data_dir = Path(state.get("data_dir", "data"))
    registry_path = _write_registry(
        records, data_dir=data_dir, session_id=manifest.session_id
    )

    ledger = Ledger(data_dir)
    ledger.create_session(manifest.session_id, state["input_manifest_ref"])
    ledger.record_assets_registered(
        manifest.session_id,
        [r.model_dump(mode="json") for r in records],
        registry_ref=str(registry_path),
        ambiguous=any("ambiguous" in w for w in report["warnings"]),
    )

    # Populate resource_refs from registered assets
    resource_refs = _populate_resource_refs_from_assets(records)

    return {
        "asset_registry_ref": str(registry_path),
        "resource_refs": resource_refs,
        "stage_outputs": {
            **(state.get("stage_outputs") or {}),
            "inventory": {
                **inventory_out,
                "status": "completed",
                "registered_assets": [r.asset_id for r in records],
            },
        },
    }


def _apply_shared_platform_override(
    manifest: InputManifest, state: TopLevelState
) -> None:
    """Apply an operator-selected platform to raw long-read assets."""
    shared = state.get("shared_parameters") or {}
    if hasattr(shared, "model_dump"):
        shared = shared.model_dump(exclude_none=True)
    if not isinstance(shared, dict):
        return
    platform = shared.get("platform")
    if not platform or platform == "unknown":
        return
    for asset in manifest.assets:
        if asset.asset_type == "raw_long_reads":
            asset.platform = platform


def commit_inventory(state: TopLevelState) -> dict:
    """Advance to the next stage based on routing decision."""
    from app.workflow import next_stage_name

    trace = append_route_trace(state, "inventory_stage")
    inventory_out = (state.get("stage_outputs") or {}).get("inventory", {})
    if inventory_out.get("entry_mode") == "existing_assembly":
        next_stage = "assembly_review"
    else:
        next_stage = next_stage_name("inventory")
    update = {
        "current_stage": next_stage,
        "supervision_state": "completed" if next_stage == "done" else "running_stage",
        "route_trace": trace,
    }
    if inventory_out.get("entry_mode") == "existing_assembly":
        update["skip_reasons"] = {
            **(state.get("skip_reasons") or {}),
            "read_qc": "existing_assembly_input",
            "genome_survey": "existing_assembly_input",
            "assembly": "existing_assembly_input",
        }
    return update


def apply_inventory_resume_decision(
    state: TopLevelState, request: Any
) -> dict[str, Any]:
    """Apply a human inventory review decision."""
    active_interrupt = state.get("active_interrupt") or {}
    review_request_id = getattr(
        request, "review_request_id", None
    ) or active_interrupt.get("review_request_id")
    review_request_hash = getattr(
        request, "review_request_hash", None
    ) or active_interrupt.get("request_hash")
    if not review_request_id or not review_request_hash:
        raise ValueError("inventory review decision is missing review request binding")
    decision_ref = persist_review_decision(
        state["data_dir"],
        session_id=state["session_id"],
        review_request_id=review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=review_request_hash,
        method_name="inventory.contract",
        artifact_type="inventory.review_decision",
    )
    Ledger(state["data_dir"]).record_review_decision(
        state["session_id"],
        "inventory",
        decision=request.decision,
        artifact_ref=decision_ref.model_dump(mode="json"),
        notes=list(getattr(request, "notes", []) or []),
        metadata=dict(getattr(request, "metadata", {}) or {}),
        decided_by=getattr(request, "decided_by", "human"),
        rationale_artifact_ref=getattr(request, "rationale_artifact_ref", None),
        review_request_id=review_request_id,
    )
    if request.decision in {"stop_human_review", "stop"}:
        return {
            "current_stage": "inventory",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": "inventory review stopped by human decision",
        }
    if request.decision not in {"continue", "accept"}:
        raise ValueError(f"unsupported inventory decision: {request.decision}")

    # The review gate temporarily changes the status to ``blocked_human``.
    # Asset registration still consumes the already-validated inventory
    # payload, so restore that control status before invoking it.  This is a
    # state transition only; no validation or selection policy is repeated.
    stage_outputs = dict(state.get("stage_outputs") or {})
    stage_outputs["inventory"] = {
        **dict(stage_outputs.get("inventory") or {}),
        "status": "validated",
    }
    validated_state = {**state, "stage_outputs": stage_outputs}
    register_update = register_assets(validated_state)
    registered_state = {**validated_state, **register_update}
    commit_update = commit_inventory(registered_state)
    outputs = dict(
        register_update.get("stage_outputs") or state.get("stage_outputs") or {}
    )
    outputs["inventory"] = {
        **dict(outputs.get("inventory") or {}),
        "review_decision_artifact_ref": decision_ref.model_dump(mode="json"),
    }
    return {
        **register_update,
        **commit_update,
        "stage_outputs": outputs,
        "active_interrupt": None,
    }


def _populate_resource_refs_from_assets(records: list[AssetRecord]) -> dict[str, dict]:
    """Populate resource_refs from registered asset records.

    Maps asset_type to logical resource names and creates ResourceRef entries.

    Args:
        records: List of registered AssetRecord objects

    Returns:
        Dictionary of resource_type -> ResourceRef dict (serialized)
    """
    resource_refs: dict[str, dict] = {}

    # Group assets by logical resource type
    for record in records:
        resource_type = None

        # Map asset_type to logical resource name
        if record.asset_type in ["raw_long_reads", "clean_long_reads", "pacbio_hifi"]:
            resource_type = "long_reads"
        elif record.asset_type in ["raw_short_dna_reads", "clean_short_dna_reads"]:
            resource_type = "short_reads"
        elif record.asset_type in [
            "hic_reads_registered",
            "hic_alignment_bam",
            "hic_merged_nodups",
        ]:
            resource_type = "hic_reads"
        elif record.asset_type == "assembly_fasta_registered":
            resource_type = "assembly"
        elif record.asset_type == "predicted_proteins":
            resource_type = "predicted_proteins"
        elif record.asset_type == "repeat_library":
            resource_type = "repeat_library"
        elif record.asset_type == "reference_proteins":
            resource_type = "reference_proteins"

        if resource_type:
            # Accumulate raw values first; ResourceRef requires a materialized
            # non-empty path list and is constructed after grouping completes.
            if resource_type not in resource_refs:
                resource_refs[resource_type] = {
                    "paths": [],
                    "metadata": {
                        "asset_ids": [],
                        "platforms": [],
                        "formats": [],
                        "asset_types": [],
                        "assets": [],
                    },
                }

            # Add this asset's path
            resource_refs[resource_type]["paths"].append(str(record.path))

            # Accumulate metadata
            resource_refs[resource_type]["metadata"]["asset_ids"].append(
                record.asset_id
            )
            if (
                record.platform
                and record.platform
                not in resource_refs[resource_type]["metadata"]["platforms"]
            ):
                resource_refs[resource_type]["metadata"]["platforms"].append(
                    record.platform
                )
            if (
                record.format
                and record.format
                not in resource_refs[resource_type]["metadata"]["formats"]
            ):
                resource_refs[resource_type]["metadata"]["formats"].append(
                    record.format
                )
            if (
                record.asset_type
                not in resource_refs[resource_type]["metadata"]["asset_types"]
            ):
                resource_refs[resource_type]["metadata"]["asset_types"].append(
                    record.asset_type
                )
            resource_refs[resource_type]["metadata"]["assets"].append(
                record.model_dump(mode="json")
            )

    return {
        resource_type: ResourceRef(
            resource_type=resource_type,
            paths=list(payload["paths"]),
            source_stage=None,
            metadata=dict(payload["metadata"]),
        ).to_dict()
        for resource_type, payload in resource_refs.items()
    }
