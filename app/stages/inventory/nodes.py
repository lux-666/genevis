"""Inventory stage nodes: manifest loading, validation, and asset registration.

Inventory performs no external side effects other than writing the asset
registry artifact and ledger events.  It does not run bioinformatics tools.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import yaml
from typing_extensions import TypedDict

from app.control.workflow_plan import advance_workflow_step, current_workflow_step_id
from app.integrity import sha256_file
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
)
from app.schemas.artifacts import ArtifactMeta, ArtifactRef, ArtifactRefJson
from app.schemas.assets import AssetRecord, AssetRegistryDocument
from app.schemas.manifest import AssetEntryInference, InputManifest
from app.schemas.resource_ref import ResourceRef
from app.schemas.run_config import RunConfig, RunConfigSharedParameters, load_run_config
from app.schemas.shared_parameters import SharedParameters
from app.schemas.state import (
    ResourceRefs,
    StateUpdate,
    TopLevelState,
    validate_top_level_state,
)
from app.stages.base import append_route_trace, resolve_run_ref
from app.stages.inventory.state import (
    read_inventory_stage_output,
    update_inventory_stage_output,
)
from app.storage.artifacts import (
    PublishedArtifact,
    publish_artifact_record,
    recover_published_artifact,
)
from app.storage.assembly_candidates import publish_registered_assembly_candidate
from app.storage.frozen_inputs import (
    RegisteredReadDeclaration,
    canonicalize_input_declarations,
)
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
    inferred: list[dict[str, Any]],
    *,
    manifest_ref: str,
    run_config_ref: str | None,
    read_declarations: tuple[RegisteredReadDeclaration, ...],
) -> list[AssetRecord]:
    """Convert canonical declarations into one record per external file."""
    if len(inferred) != len(manifest.assets):
        raise ValueError(
            "manifest inference entry count does not match manifest asset count"
        )
    reads_by_path = {declaration.path: declaration for declaration in read_declarations}
    records: list[AssetRecord] = []
    seen_asset_ids: dict[str, str] = {}
    for entry_ordinal, spec in enumerate(manifest.assets):
        entry = AssetEntryInference.model_validate(inferred[entry_ordinal])
        if (
            entry.manifest_entry_ordinal != entry_ordinal
            or entry.asset_type != spec.asset_type
            or len(entry.path_facts) != len(spec.paths)
        ):
            raise ValueError(f"manifest inference mismatch at assets[{entry_ordinal}]")
        pair_roles = {
            str(Path(path).expanduser().resolve(strict=False)): (pair_index, mate)
            for pair_index, pair in enumerate(spec.paired_reads)
            for mate, path in (("R1", pair.r1), ("R2", pair.r2))
        }
        for path_ordinal, declared_path in enumerate(spec.paths):
            fact = entry.path_facts[path_ordinal]
            if fact.path_ordinal != path_ordinal or fact.declared_path != declared_path:
                raise ValueError(
                    f"manifest inference path mismatch at "
                    f"assets[{entry_ordinal}].paths[{path_ordinal}]"
                )
            path = Path(fact.canonical_path)
            asset_id = _asset_id(manifest.session_id, path)
            _claim_asset_id(seen_asset_ids, asset_id, path)
            pair_role = pair_roles.get(str(path))
            declaration = reads_by_path.get(str(path))
            if spec.asset_type in {"raw_long_reads", "clean_long_reads"}:
                if declaration is None:
                    raise ValueError(f"missing canonical read declaration for {path}")
                if (
                    declaration.manifest_entry_ordinal != entry_ordinal
                    or declaration.path_ordinal != path_ordinal
                ):
                    raise ValueError(f"read declaration ordinal mismatch for {path}")
            metadata: dict[str, Any] = {
                **spec.metadata,
                "layout": (
                    declaration.layout
                    if declaration is not None
                    else entry.inferred_layout
                ),
                "compression": (
                    declaration.compression
                    if declaration is not None
                    else fact.compression
                ),
                "manifest_entry_ordinal": entry_ordinal,
                "path_ordinal": path_ordinal,
            }
            if pair_role is not None:
                metadata["pair_index"] = pair_role[0]
                metadata["mate"] = pair_role[1]
            records.append(
                AssetRecord(
                    asset_id=asset_id,
                    asset_type=spec.asset_type,
                    path=path,
                    sample_id=(
                        declaration.sample_id
                        if declaration is not None
                        else spec.sample_id or manifest.sample_id
                    ),
                    platform=(
                        declaration.platform
                        if declaration is not None
                        else fact.platform
                    ),
                    format=(
                        declaration.format if declaration is not None else fact.format
                    ),
                    checksum=sha256_file(path),
                    checksum_status="ready",
                    size_bytes=(
                        declaration.size_bytes
                        if declaration is not None
                        else fact.size_bytes
                    ),
                    verified_at=datetime.now(timezone.utc),
                    allowed_stages=[],
                    metadata=metadata,
                    provenance=_asset_provenance(
                        declaration=declaration,
                        fallback_declared_path=declared_path,
                        fallback_field=(
                            f"assets[{entry_ordinal}].paths[{path_ordinal}]"
                        ),
                        platform_resolution_source=(
                            declaration.platform_resolution_source
                            if declaration is not None
                            else fact.platform_resolution_source
                        ),
                        manifest_ref=manifest_ref,
                        run_config_ref=run_config_ref,
                    ),
                )
            )

    for declaration in read_declarations:
        if declaration.manifest_entry_ordinal is not None:
            continue
        path = Path(declaration.path)
        asset_id = _asset_id(manifest.session_id, path)
        _claim_asset_id(seen_asset_ids, asset_id, path)
        records.append(
            AssetRecord(
                asset_id=asset_id,
                asset_type=declaration.asset_type,
                path=path,
                sample_id=declaration.sample_id,
                platform=declaration.platform,
                format=declaration.format,
                checksum=sha256_file(path),
                checksum_status="ready",
                size_bytes=declaration.size_bytes,
                verified_at=datetime.now(timezone.utc),
                allowed_stages=[],
                metadata={
                    **declaration.metadata,
                    "layout": declaration.layout,
                    "compression": declaration.compression,
                },
                provenance=_asset_provenance(
                    declaration=declaration,
                    fallback_declared_path=declaration.path,
                    fallback_field="run_config",
                    platform_resolution_source=(declaration.platform_resolution_source),
                    manifest_ref=None,
                    run_config_ref=run_config_ref,
                ),
            )
        )
    return records


def _claim_asset_id(
    seen_asset_ids: dict[str, str],
    asset_id: str,
    path: Path,
) -> None:
    canonical_path = str(path.resolve(strict=False))
    previous_path = seen_asset_ids.get(asset_id)
    if previous_path is not None:
        if previous_path == canonical_path:
            raise ValueError(f"duplicate asset record for {canonical_path}")
        raise ValueError(
            f"generated asset ID collision between {previous_path} and {canonical_path}"
        )
    seen_asset_ids[asset_id] = canonical_path


def _asset_provenance(
    *,
    declaration: RegisteredReadDeclaration | None,
    fallback_declared_path: str,
    fallback_field: str,
    platform_resolution_source: str,
    manifest_ref: str | None,
    run_config_ref: str | None,
) -> dict[str, Any]:
    sources = (
        [
            {
                "declaration_source": source.declaration_source,
                "declaration_field": source.declaration_field,
                "declared_path": source.declared_path,
            }
            for source in declaration.sources
        ]
        if declaration is not None
        else [
            {
                "declaration_source": "manifest",
                "declaration_field": fallback_field,
                "declared_path": fallback_declared_path,
            }
        ]
    )
    provenance: dict[str, Any] = {
        "declarations": sources,
        "platform_resolution_source": platform_resolution_source,
    }
    if manifest_ref is not None:
        provenance["manifest_ref"] = manifest_ref
    if run_config_ref is not None and any(
        source["declaration_source"] == "run_config" for source in sources
    ):
        provenance["run_config_ref"] = run_config_ref
    return provenance


def _asset_id(session_id: str, path: Path) -> str:
    """Return a stable opaque identity for one canonical external path."""

    identity = f"{session_id}\0{path.resolve(strict=False)}"
    return f"asset_{uuid5(NAMESPACE_URL, identity).hex}"


def _entry_mode(manifest: InputManifest) -> str:
    """Return the entry mode implied by manifest assets."""
    asset_types = {asset.asset_type for asset in manifest.assets}
    if "assembly_fasta_registered" in asset_types:
        return "existing_assembly"
    return "raw_inputs"


def _recover_registry_if_unchanged(
    paths: SessionPaths,
    session_id: str,
    destination: Path,
    records: list[AssetRecord],
) -> PublishedArtifact | None:
    """Return the existing artifact when all stable asset facts are unchanged.

    Verification timestamps are attempt-local; every other record field,
    including ordering, provenance, platform, size, and checksum, must match.
    This keeps replay idempotent without accepting a changed declaration.
    """
    try:
        existing_doc = AssetRegistryDocument.model_validate(
            json.loads(destination.read_text(encoding="utf-8"))
        )
    except Exception:
        return None
    existing_payload = [
        record.model_dump(mode="json", exclude={"verified_at"})
        for record in existing_doc.assets
    ]
    new_payload = [
        record.model_dump(mode="json", exclude={"verified_at"}) for record in records
    ]
    if existing_payload != new_payload:
        return None
    return recover_published_artifact(
        paths.run_dir,
        session_id,
        destination,
        stage="inventory",
        method_name="inventory.register_assets",
        artifact_type="asset_registry",
    )


def _write_registry(
    records: list[AssetRecord],
    *,
    data_dir: Path,
    session_id: str,
    workflow_step_id: str,
) -> PublishedArtifact:
    """Publish the asset registry through the shared immutable boundary."""
    paths = SessionPaths.from_run_dir(data_dir)
    destination = paths.workflow_step_artifact_path(
        workflow_step_id,
        "inventory",
        "asset_registry",
        "registry.json",
    )
    # If the destination already exists, reuse it when the underlying asset
    # checksums have not changed.  This makes the node idempotent: a replayed
    # run with identical inputs returns the same PublishedArtifact without
    # attempting to overwrite an immutable path.
    if destination.is_file():
        recovered = _recover_registry_if_unchanged(
            paths, session_id, destination, records
        )
        if recovered is not None:
            return recovered
    source = paths.workflow_step_work_path(
        workflow_step_id,
        "inventory",
        "registry.json",
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    payload = AssetRegistryDocument(
        session_id=session_id,
        assets=records,
    ).model_dump(mode="json")
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
    )


def enter_inventory(state: TopLevelState) -> StateUpdate:
    """Mark inventory as running.  No side effects."""
    return {"supervision_state": "running_stage"}


def validate_inputs(state: TopLevelState) -> StateUpdate:
    """Load and validate the input manifest.

    Returns state updates that the routing node will use to decide
    success, failure, or review-required.
    """
    manifest_ref = state.get("input_manifest_ref")
    if not manifest_ref:
        return update_inventory_stage_output(
            state,
            {
                "status": "failed",
                "reason": "missing input_manifest_ref",
            },
        )

    try:
        manifest = _load_manifest(str(resolve_run_ref(state, manifest_ref)))
        run_config, _ = _load_inventory_run_config(state)
        declarations = canonicalize_input_declarations(manifest, run_config)
        manifest = declarations.manifest
        report = declarations.manifest_report
        canonical_shared = _canonical_shared_parameters(
            state,
            manifest,
            run_config,
        )
    except Exception as exc:
        return update_inventory_stage_output(
            state,
            {
                "status": "failed",
                "reason": f"manifest load error: {exc}",
            },
        )

    update = update_inventory_stage_output(
        state,
        {
            "status": "validated",
            "valid": report["valid"],
            "errors": report["errors"],
            "warnings": report["warnings"],
            "asset_count": (
                sum(len(asset.paths) for asset in manifest.assets)
                + sum(
                    declaration.manifest_entry_ordinal is None
                    for declaration in declarations.read_declarations
                )
            ),
            "entry_mode": _entry_mode(manifest),
            "genome_size_estimate": manifest.genome_size_estimate,
            "target_assembly_level": manifest.target_assembly_level,
        },
    )
    if canonical_shared is not None:
        update["shared_parameters"] = canonical_shared
    return update


def _canonical_shared_parameters(
    state: TopLevelState,
    manifest: InputManifest,
    run_config: RunConfig,
) -> dict[str, Any] | None:
    """Reconcile immutable run-config facts with the runtime projection."""

    raw_shared = state.get("shared_parameters")
    if isinstance(raw_shared, SharedParameters):
        raw_shared = raw_shared.model_dump(exclude_none=True)
    configured = (
        run_config.shared_parameters.model_dump(exclude_none=True)
        if run_config.shared_parameters is not None
        else {}
    )
    if raw_shared is None:
        shared = dict(configured)
    elif isinstance(raw_shared, dict):
        shared = dict(raw_shared)
    else:
        raise ValueError("shared_parameters must be a mapping")

    for key, configured_value in configured.items():
        runtime_value = shared.get(key)
        if runtime_value is not None and runtime_value != configured_value:
            raise ValueError(
                f"runtime shared_parameters.{key} conflicts with frozen run-config"
            )
        shared[key] = configured_value

    manifest_size = manifest.genome_size_estimate
    if manifest_size is not None:
        existing_size = shared.get("estimated_genome_size")
        if existing_size is not None and existing_size != manifest_size:
            raise ValueError(
                "runtime shared_parameters.estimated_genome_size conflicts with "
                "manifest.genome_size_estimate"
            )
        shared["estimated_genome_size"] = manifest_size
    if not shared:
        return None
    return SharedParameters.model_validate(shared).model_dump(exclude_none=True)


def register_assets(state: TopLevelState) -> StateUpdate:
    """Register assets, write registry artifact, and record ledger events."""
    inventory_out = read_inventory_stage_output(state)
    if inventory_out.get("status") != "validated" or not inventory_out.get("valid"):
        return {}

    manifest_ref = state["input_manifest_ref"]
    manifest = _load_manifest(str(resolve_run_ref(state, manifest_ref)))
    run_config, run_config_ref = _load_inventory_run_config(state)
    declarations = canonicalize_input_declarations(manifest, run_config)
    manifest = declarations.manifest
    report = declarations.manifest_report
    if not report["valid"]:
        raise ValueError(
            "input declarations changed after validation: "
            + "; ".join(report["errors"])
        )
    inferred = report["inferred"]

    records = _build_asset_records(
        manifest,
        inferred,
        manifest_ref=manifest_ref,
        run_config_ref=run_config_ref,
        read_declarations=declarations.read_declarations,
    )
    data_dir = Path(state.get("data_dir", "data"))
    workflow_step_id = current_workflow_step_id(
        state,
        expected_stage="inventory",
    )
    published_registry = _write_registry(
        records,
        data_dir=data_dir,
        session_id=manifest.session_id,
        workflow_step_id=workflow_step_id,
    )
    registry_ref = cast(ArtifactRefJson, published_registry.ref.model_dump(mode="json"))

    ledger = Ledger(data_dir)
    ledger.create_session(manifest.session_id, state["input_manifest_ref"])
    ledger.record_assets_registered(
        manifest.session_id,
        [r.model_dump(mode="json") for r in records],
        registry_ref=registry_ref,
        ambiguous=any("ambiguous" in w for w in report["warnings"]),
        workflow_step_id=workflow_step_id,
    )

    # Populate resource_refs from registered assets
    resource_refs = _populate_resource_refs_from_assets(
        records,
        asset_registry_ref=registry_ref,
    )
    registered_assemblies = [
        record for record in records if record.asset_type == "assembly_fasta_registered"
    ]
    if len(registered_assemblies) > 1:
        raise ValueError(
            "single-backbone inventory accepts exactly one registered assembly"
        )
    if registered_assemblies:
        registered_assembly = registered_assemblies[0]
        if (
            registered_assembly.checksum_status != "ready"
            or registered_assembly.checksum is None
            or registered_assembly.size_bytes is None
        ):
            raise ValueError("registered assembly lacks immutable byte evidence")
        bound_candidate = publish_registered_assembly_candidate(
            data_dir,
            manifest.session_id,
            workflow_step_id=workflow_step_id,
            asset_id=registered_assembly.asset_id,
            source_path=registered_assembly.path,
            expected_sha256=registered_assembly.checksum,
            expected_size_bytes=registered_assembly.size_bytes,
            asset_registry_ref=published_registry.ref,
        )
        assembly_resource = ResourceRef.from_dict(resource_refs["assembly"])
        source_metadata = dict(assembly_resource.metadata)
        source_asset_ids = list(source_metadata.pop("asset_ids", []) or [])
        source_registry_ref = source_metadata.pop("asset_registry_ref", None)
        resource_refs["assembly"] = ResourceRef(
            resource_type="assembly",
            paths=[str(bound_candidate.primary_output_path)],
            source_stage="inventory_registered_assembly",
            workflow_step_id=workflow_step_id,
            metadata={
                **source_metadata,
                "source_asset_ids": source_asset_ids,
                "source_asset_registry_ref": source_registry_ref,
                "candidate_bundle_ref": bound_candidate.ref.model_dump(mode="json"),
                "primary_output_ref": (
                    bound_candidate.primary_output_ref.model_dump(mode="json")
                ),
            },
        ).to_dict()
        ledger.record_event(
            manifest.session_id,
            "registered_assembly_candidate_bound",
            {
                "asset_id": registered_assembly.asset_id,
                "candidate_bundle_ref": bound_candidate.ref.model_dump(mode="json"),
                "primary_output_ref": (
                    bound_candidate.primary_output_ref.model_dump(mode="json")
                ),
            },
        )

    update = update_inventory_stage_output(
        state,
        {
            "status": "completed",
            "registered_assets": [r.asset_id for r in records],
        },
    )
    update["asset_registry_ref"] = dict(registry_ref)
    update["resource_refs"] = resource_refs
    return update


def _load_inventory_run_config(
    state: TopLevelState,
) -> tuple[RunConfig, str | None]:
    """Load the run-owned declaration source used by frozen validation."""

    paths = SessionPaths.from_run_dir(Path(state.get("data_dir", "data")))
    if paths.run_config_path.is_file():
        return (
            load_run_config(paths.run_config_path),
            paths.to_ref(paths.run_config_path),
        )
    raw_shared = state.get("shared_parameters")
    if isinstance(raw_shared, SharedParameters):
        shared = raw_shared.model_dump(mode="json", exclude_none=True)
    elif isinstance(raw_shared, dict):
        shared = dict(raw_shared)
    elif raw_shared is None:
        shared = None
    else:
        raise ValueError("shared_parameters must be a mapping")
    return (
        RunConfig(
            requested_stages=[],
            shared_parameters=(
                RunConfigSharedParameters.model_validate(shared)
                if shared is not None
                else None
            ),
        ),
        None,
    )


def commit_inventory(state: TopLevelState) -> StateUpdate:
    """Complete inventory and advance one frozen workflow occurrence."""
    trace = append_route_trace(state, "inventory_stage")
    return {
        **advance_workflow_step(state, "inventory"),
        "route_trace": trace,
    }


def apply_inventory_resume_decision(state: TopLevelState, request: Any) -> StateUpdate:
    """Apply a human inventory review decision."""
    review_request_id, review_request_hash, review_request_ref = (
        _inventory_review_binding(state, request)
    )
    decision_ref = persist_review_decision(
        state["data_dir"],
        session_id=state["session_id"],
        review_request_id=review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=review_request_hash,
        request_ref=review_request_ref,
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
    restored = update_inventory_stage_output(state, {"status": "validated"})
    validated_state = validate_top_level_state({**state, **dict(restored)})
    register_update = register_assets(validated_state)
    registered_state = validate_top_level_state(
        {**validated_state, **dict(register_update)}
    )
    commit_update = commit_inventory(registered_state)
    decision_update = update_inventory_stage_output(
        registered_state,
        {
            "review_decision_artifact_ref": decision_ref.model_dump(mode="json"),
        },
    )
    result: StateUpdate = {}
    result.update(register_update)
    result.update(commit_update)
    result["stage_outputs"] = decision_update["stage_outputs"]
    result["active_interrupt"] = None
    return result


def _inventory_review_binding(
    state: TopLevelState, request: Any
) -> tuple[str, str, ArtifactRef]:
    """Return the verified, immutable request binding for inventory decisions."""

    active_interrupt: Mapping[str, Any] = state.get("active_interrupt") or {}
    inventory_out = read_inventory_stage_output(state)
    review_request_id = getattr(
        request, "review_request_id", None
    ) or active_interrupt.get("review_request_id")
    review_request_hash = getattr(
        request, "review_request_hash", None
    ) or active_interrupt.get("request_hash")
    request_ref_value = active_interrupt.get("request_ref") or inventory_out.get(
        "review_request_artifact_ref"
    )
    if not review_request_id or not review_request_hash or request_ref_value is None:
        raise ValueError("inventory review decision is missing review request binding")
    try:
        request_ref = ArtifactRef.model_validate(request_ref_value)
        request_record = load_review_request(
            state["data_dir"], request_ref, session_id=state["session_id"]
        )
    except (ReviewPersistenceError, ValueError, TypeError) as exc:
        raise ValueError("inventory review request is missing or changed") from exc
    workflow_step_id = active_interrupt.get(
        "workflow_step_id"
    ) or current_workflow_step_id(state, expected_stage="inventory")
    if (
        request_ref.sha256 != str(review_request_hash)
        or request_record.get("review_request_id") != str(review_request_id)
        or request_record.get("stage") != "inventory"
        or request_record.get("gate_id") != "inventory_review"
        or request_record.get("workflow_step_id") != workflow_step_id
    ):
        raise ValueError("inventory review request binding mismatch")
    return str(review_request_id), str(review_request_hash), request_ref


class _ResourceMetadataAccumulator(TypedDict):
    asset_ids: list[str]
    platforms: list[str]
    formats: list[str]
    asset_types: list[str]
    asset_registry_ref: ArtifactRefJson
    member_count: int
    pair_count: int


class _ResourceAccumulator(TypedDict):
    paths: list[str]
    metadata: _ResourceMetadataAccumulator


def _populate_resource_refs_from_assets(
    records: list[AssetRecord],
    *,
    asset_registry_ref: ArtifactRefJson,
) -> ResourceRefs:
    """Populate resource_refs from registered asset records.

    Maps asset_type to logical resource names and creates ResourceRef entries.

    Args:
        records: List of registered AssetRecord objects

    Returns:
        Dictionary of resource_type -> ResourceRef dict (serialized)
    """
    resource_refs: dict[str, _ResourceAccumulator] = {}

    # Group assets by logical resource type
    for record in records:
        resource_type = None

        # Map asset_type to logical resource name
        if record.asset_type in ["raw_long_reads", "clean_long_reads"]:
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
                        "asset_registry_ref": asset_registry_ref,
                        "member_count": 0,
                        "pair_count": 0,
                    },
                }

            # Add this asset's path
            resource_refs[resource_type]["paths"].append(str(record.path))

            # Accumulate metadata
            resource_refs[resource_type]["metadata"]["asset_ids"].append(
                record.asset_id
            )
            resource_refs[resource_type]["metadata"]["member_count"] += 1
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
            if resource_type == "hic_reads" and record.metadata.get("mate") == "R1":
                resource_refs[resource_type]["metadata"]["pair_count"] += 1

    return {
        resource_type: ResourceRef(
            resource_type=resource_type,
            paths=list(payload["paths"]),
            source_stage=None,
            workflow_step_id=None,
            metadata=dict(payload["metadata"]),
        ).to_dict()
        for resource_type, payload in resource_refs.items()
    }
