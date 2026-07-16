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
from app.review.persistence import persist_review_decision
from app.stages.base import resolve_run_ref
from app.storage.paths import SessionPaths


def _load_manifest(manifest_ref: str) -> InputManifest:
    """Load an InputManifest from a YAML or JSON file path.

    Relative asset paths are resolved against the manifest's directory first
    and cwd second, then stored as absolute paths so downstream attempts that
    chdir into their working directory can still find the files.
    """
    path = Path(manifest_ref)
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix == ".json":
            raw = json.load(handle)
        else:
            raw = yaml.safe_load(handle) or {}
    manifest_dir = path.resolve().parent
    for asset in raw.get("assets") or []:
        resolved: list[str] = []
        for p in asset.get("paths") or []:
            resolved.append(_absolutize_asset_path(str(p), manifest_dir=manifest_dir))
        if resolved:
            asset["paths"] = resolved
    return InputManifest.model_validate(raw)


def _absolutize_asset_path(raw_path: str, *, manifest_dir: Path) -> str:
    """Resolve a manifest-declared path to an absolute filesystem path.

    Absolute paths are left as-is (so provenance keeps the operator's exact
    spelling, and macOS ``/var/...`` vs ``/private/var/...`` canonicalisation
    does not surprise downstream consumers). Relative paths are anchored to
    the manifest directory first, then cwd as a fallback.
    """
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return str(candidate)
    manifest_relative = (manifest_dir / candidate).resolve(strict=False)
    if manifest_relative.exists():
        return str(manifest_relative)
    cwd_relative = (Path.cwd() / candidate).resolve(strict=False)
    if cwd_relative.exists():
        return str(cwd_relative)
    return str(manifest_relative)


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
    supporting = {"repeat_library", "gene_models", "predicted_proteins", "transcripts_fasta"}
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
    """Write the asset registry JSON artifact and return its path."""
    registry_dir = SessionPaths.from_run_dir(data_dir).stage_artifacts_dir("asset_registry")
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
        manifest = _load_manifest(str(resolve_run_ref(state, manifest_ref)))
    except Exception as exc:
        return {
            "stage_outputs": {
                **(state.get("stage_outputs") or {}),
                "inventory": {"status": "failed", "reason": f"manifest load error: {exc}"},
            },
        }

    _apply_shared_platform_override(manifest, state)
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
                "genome_size_estimate": manifest.genome_size_estimate,
                "target_assembly_level": manifest.target_assembly_level,
            },
        },
    }


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


def _apply_shared_platform_override(manifest: InputManifest, state: TopLevelState) -> None:
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


def apply_inventory_resume_decision(state: TopLevelState, request: Any) -> dict[str, Any]:
    """Apply a human inventory review decision."""
    active_interrupt = state.get("active_interrupt") or {}
    review_request_id = getattr(request, "review_request_id", None) or active_interrupt.get(
        "review_request_id"
    )
    review_request_hash = getattr(request, "review_request_hash", None) or active_interrupt.get(
        "request_hash"
    )
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
    outputs = dict(register_update.get("stage_outputs") or state.get("stage_outputs") or {})
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
