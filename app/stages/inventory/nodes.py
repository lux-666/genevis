"""Inventory stage nodes: manifest loading, validation, and asset registration.

Inventory performs no external side effects other than writing the asset
registry artifact and ledger events.  It does not run bioinformatics tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from app.control.ledger import Ledger
from app.schemas.assets import AssetRecord
from app.schemas.manifest import InputManifest, validate_manifest
from app.schemas.state import TopLevelState


def _load_manifest(manifest_ref: str) -> InputManifest:
    """Load an InputManifest from a YAML or JSON file path."""
    path = Path(manifest_ref)
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix == ".json":
            raw = json.load(handle)
        else:
            raw = yaml.safe_load(handle) or {}
    return InputManifest.model_validate(raw)


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
                        "compression": props.get("inferred_compression", spec.compression),
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
    if asset_types == {"assembly_fasta_registered"}:
        return "existing_assembly"
    return "raw_inputs"


def _write_registry(
    records: list[AssetRecord],
    *,
    data_dir: Path,
    session_id: str,
) -> Path:
    """Write the asset registry JSON artifact and return its path."""
    registry_dir = data_dir / "artifacts" / "asset_registry" / session_id
    registry_dir.mkdir(parents=True, exist_ok=True)
    registry_path = registry_dir / "registry.json"
    payload = {
        "session_id": session_id,
        "assets": [r.model_dump(mode="json") for r in records],
    }
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return registry_path


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
                "inventory": {"status": "failed", "reason": "missing input_manifest_ref"},
            },
        }

    try:
        manifest = _load_manifest(manifest_ref)
    except Exception as exc:
        return {
            "stage_outputs": {
                **(state.get("stage_outputs") or {}),
                "inventory": {"status": "failed", "reason": f"manifest load error: {exc}"},
            },
        }

    report = validate_manifest(manifest)
    return {
        "stage_outputs": {
            **(state.get("stage_outputs") or {}),
            "inventory": {
                "status": "validated",
                "valid": report["valid"],
                "errors": report["errors"],
                "warnings": report["warnings"],
                "asset_count": len(manifest.assets),
                "entry_mode": _entry_mode(manifest),
            },
        },
    }


def register_assets(state: TopLevelState) -> dict:
    """Register assets, write registry artifact, and record ledger events."""
    inventory_out = (state.get("stage_outputs") or {}).get("inventory", {})
    if inventory_out.get("status") != "validated" or not inventory_out.get("valid"):
        return {}

    manifest_ref = state["input_manifest_ref"]
    manifest = _load_manifest(manifest_ref)
    report = validate_manifest(manifest)
    inferred = report["inferred"]

    records = _build_asset_records(manifest, inferred, manifest_ref=manifest_ref)
    data_dir = Path(state.get("data_dir", "data"))
    registry_path = _write_registry(records, data_dir=data_dir, session_id=manifest.session_id)

    ledger = Ledger(data_dir)
    ledger.create_session(manifest.session_id, state["input_manifest_ref"])
    ledger.record_assets_registered(
        manifest.session_id,
        [r.model_dump(mode="json") for r in records],
        registry_ref=str(registry_path),
        ambiguous=any("ambiguous" in w for w in report["warnings"]),
    )

    return {
        "asset_registry_ref": str(registry_path),
        "stage_outputs": {
            **(state.get("stage_outputs") or {}),
            "inventory": {
                **inventory_out,
                "status": "completed",
                "registered_assets": [r.asset_id for r in records],
            },
        },
    }


def commit_inventory(state: TopLevelState) -> dict:
    """Advance to the next stage based on routing decision."""
    from app.graph.routing import next_stage_name

    trace = [*state.get("route_trace", []), "inventory_stage"]
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
