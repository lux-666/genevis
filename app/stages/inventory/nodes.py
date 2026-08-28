"""Inventory stage nodes: manifest loading, validation, and asset registration.

Inventory performs no external side effects other than writing the asset
registry artifact and ledger events.  It does not run bioinformatics tools.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from pydantic import (
    JsonValue as PydanticJsonValue,
)
from typing_extensions import TypedDict

from app.control.errors import ResumeValidationError
from app.control.workflow_plan import (
    advance_workflow_step,
    assembly_delivery,
    current_workflow_step_id,
    fail_workflow_step,
    seed_current_workflow_step,
)
from app.integrity import sha256_file
from app.review.application import (
    PreparedReviewApplication,
    ReviewIntentV1,
    VerifiedReviewApplication,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
)
from app.schemas.artifacts import ArtifactMeta, ArtifactRef, ArtifactRefJson
from app.schemas.assets import AssetRecord, AssetRegistryDocument
from app.schemas.manifest import AssetEntryInference, InputManifest
from app.schemas.resource_ref import ResourceRef
from app.schemas.run_config import RunConfig
from app.schemas.shared_parameters import SharedParameters
from app.schemas.state import (
    ResourceDelivery,
    ResourceRefs,
    StateUpdate,
    TopLevelState,
    validate_top_level_state,
)
from app.stages.base import append_route_trace
from app.stages.inventory.state import (
    read_inventory_stage_output,
    update_inventory_stage_output,
)
from app.storage.artifacts import (
    PublishedArtifact,
    publish_artifact_record,
    recover_published_artifact,
)
from app.storage.assembly_candidates import (
    load_executed_candidate_bundle,
    publish_registered_assembly_candidate,
)
from app.storage.frozen_inputs import (
    FrozenInputBundle,
    RegisteredReadDeclaration,
    validate_frozen_inputs,
)
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths


class InventoryResourceEffectV1(BaseModel):
    """One bounded inventory resource projection prepared for checkpoint apply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_name: str = Field(min_length=1, max_length=128)
    resource_type: str = Field(min_length=1, max_length=128)
    paths: tuple[str, ...] = Field(min_length=1, max_length=10_000)
    source_stage: str | None = None
    workflow_step_id: str | None = None
    metadata: dict[str, PydanticJsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_resource_ref(self) -> "InventoryResourceEffectV1":
        ResourceRef.from_dict(
            {
                "resource_type": self.resource_type,
                "paths": list(self.paths),
                "source_stage": self.source_stage,
                "workflow_step_id": self.workflow_step_id,
                "metadata": self.metadata,
            }
        )
        return self


class InventoryReviewApplicationEffectV1(BaseModel):
    """Typed durable effect for the ``inventory_review`` gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    decision: Literal["continue", "stop_human_review"]
    asset_registry_ref: ArtifactRef | None = None
    registered_asset_ids: tuple[str, ...] = Field(default=(), max_length=10_000)
    resources: tuple[InventoryResourceEffectV1, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_decision_effect(self) -> "InventoryReviewApplicationEffectV1":
        if self.decision == "continue":
            if self.asset_registry_ref is None:
                raise ValueError(
                    "inventory continue effect requires asset_registry_ref"
                )
            if not self.registered_asset_ids:
                raise ValueError("inventory continue effect requires registered assets")
        elif (
            self.asset_registry_ref is not None
            or self.registered_asset_ids
            or self.resources
        ):
            raise ValueError("inventory stop effect must not include registration data")
        return self


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
    """Mark Inventory as entered and running for standalone-stage execution."""
    return {
        **seed_current_workflow_step(state),
        "supervision_state": "running_stage",
    }


def validate_inputs(
    state: TopLevelState,
    *,
    verified_inputs: FrozenInputBundle | None = None,
) -> StateUpdate:
    """Load and validate the input manifest.

    Returns state updates that the routing node will use to decide
    success, failure, or review-required.
    """
    try:
        bundle = _require_verified_inputs(verified_inputs, state=state)
        manifest = bundle.declarations.manifest
        run_config = bundle.run_config
        declarations = bundle.declarations
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

    from app.reference.ncbi import species_name_from_metadata

    manifest_species = species_name_from_metadata(manifest.user_metadata)
    if manifest_species is not None:
        species_facts = dict(shared.get("species_facts") or {})
        configured_species = species_facts.get("scientific_name")
        if configured_species is not None and configured_species != manifest_species:
            raise ValueError(
                "runtime shared_parameters.species_facts.scientific_name conflicts "
                "with manifest target_species"
            )
        species_facts["scientific_name"] = manifest_species
        shared["species_facts"] = species_facts

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


def register_assets(
    state: TopLevelState,
    *,
    verified_inputs: FrozenInputBundle | None = None,
) -> StateUpdate:
    """Register assets, write registry artifact, and record ledger events."""
    inventory_out = read_inventory_stage_output(state)
    if inventory_out.get("status") != "validated" or not inventory_out.get("valid"):
        return {}

    bundle = _require_verified_inputs(verified_inputs, state=state)
    manifest_ref = bundle.paths.to_ref(bundle.paths.input_manifest_path)
    run_config_ref = bundle.paths.to_ref(bundle.paths.run_config_path)
    manifest = bundle.declarations.manifest
    declarations = bundle.declarations
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
            source_stage="inventory",
            workflow_step_id=workflow_step_id,
            metadata={
                **source_metadata,
                "source_asset_ids": source_asset_ids,
                "source_asset_registry_ref": source_registry_ref,
                "source_description": "inventory_registered_assembly",
                "candidate_bundle_ref": bound_candidate.ref.model_dump(mode="json"),
                "primary_output_ref": (
                    bound_candidate.primary_output_ref.model_dump(mode="json")
                ),
            },
        ).to_dict()
        ledger.record_occurrence_event(
            manifest.session_id,
            "registered_assembly_candidate_bound",
            {
                "asset_id": registered_assembly.asset_id,
                "candidate_bundle_ref": bound_candidate.ref.model_dump(mode="json"),
                "primary_output_ref": (
                    bound_candidate.primary_output_ref.model_dump(mode="json")
                ),
            },
            stage="inventory",
            workflow_step_id=workflow_step_id,
            subject_id=registered_assembly.asset_id,
            idempotency_key=(
                f"registered-assembly-candidate:{workflow_step_id}:"
                f"{registered_assembly.asset_id}"
            ),
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


def _require_verified_inputs(
    verified_inputs: FrozenInputBundle | None,
    *,
    state: TopLevelState,
) -> FrozenInputBundle:
    """Return the bundle only when it belongs to this exact runtime state."""

    if verified_inputs is None:
        raise ValueError(
            "Inventory requires a verified FrozenInputBundle; graph construction "
            "must validate frozen inputs before execution"
        )
    state_session_id = state.get("session_id")
    if verified_inputs.paths.session_id != state_session_id:
        raise ValueError(
            "Inventory verified inputs session_id does not match runtime state: "
            f"{verified_inputs.paths.session_id!r} != {state_session_id!r}"
        )
    runtime_paths = SessionPaths.from_run_dir(state.get("data_dir", "data"))
    if verified_inputs.paths.run_dir != runtime_paths.run_dir:
        raise ValueError(
            "Inventory verified inputs run_dir does not match runtime state: "
            f"{verified_inputs.paths.run_dir} != {runtime_paths.run_dir}"
        )
    return verified_inputs


def commit_inventory(state: TopLevelState) -> StateUpdate:
    """Complete inventory and advance one frozen workflow occurrence."""
    try:
        deliveries = _registered_assembly_deliveries(state)
    except (OSError, TypeError, ValueError) as exc:
        reason = f"inventory registered assembly delivery is invalid: {exc}"
        return {
            **update_inventory_stage_output(
                state,
                {"status": "failed", "reason": reason},
            ),
            **fail_workflow_step(state, "inventory", reason),
        }
    trace = append_route_trace(state, "inventory_stage")
    return {
        **advance_workflow_step(
            state,
            "inventory",
            outcome="completed",
            deliveries=deliveries,
        ),
        "route_trace": trace,
    }


def _registered_assembly_deliveries(
    state: TopLevelState,
) -> tuple[ResourceDelivery, ...]:
    """Return Inventory's materialized registered-assembly delivery, if any."""

    raw_resource = (state.get("resource_refs") or {}).get("assembly")
    if raw_resource is None:
        return ()
    if not isinstance(raw_resource, Mapping):
        raise ValueError("inventory assembly resource projection must be a mapping")
    resource = ResourceRef.from_dict(raw_resource)
    if resource.source_stage != "inventory":
        return ()
    candidate_bundle_ref = resource.metadata.get("candidate_bundle_ref")
    primary_output_ref = resource.metadata.get("primary_output_ref")
    if not isinstance(candidate_bundle_ref, Mapping) or not isinstance(
        primary_output_ref, Mapping
    ):
        raise ValueError(
            "registered inventory assembly is missing its candidate relation"
        )
    verified = load_executed_candidate_bundle(
        state.get("data_dir", "data"),
        state["session_id"],
        dict(candidate_bundle_ref),
        expected_method_name="inventory.registered_assembly",
    )
    if verified.primary_output_ref.model_dump(mode="json") != dict(primary_output_ref):
        raise ValueError(
            "registered inventory assembly primary output differs from its candidate"
        )
    if len(resource.paths) != 1 or Path(resource.paths[0]).resolve(
        strict=False
    ) != verified.primary_output_path.resolve(strict=False):
        raise ValueError(
            "registered inventory assembly path differs from its candidate output"
        )
    return (
        assembly_delivery(
            verified.ref.model_dump(mode="json"),
            verified.primary_output_ref.model_dump(mode="json"),
            delivery_mode="materialized",
        ),
    )


def prepare_inventory_review_application(
    state: TopLevelState,
    intent: ReviewIntentV1,
) -> PreparedReviewApplication:
    """Durably prepare the inventory gate effect before checkpoint apply."""

    _validate_inventory_intent(state, intent)
    review_request_id, review_request_hash, review_request_ref, request_record = (
        _inventory_review_binding(state, intent)
    )
    request = validate_inventory_review_intent_request(
        state, intent.request, request_record
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
    if request.decision == "stop_human_review":
        return PreparedReviewApplication(
            effect_kind="inventory.review_application.v1",
            effect=InventoryReviewApplicationEffectV1(
                decision="stop_human_review",
            ),
            decision_ref=decision_ref,
        )

    # Resume preparation independently re-verifies the durable frozen-input
    # contract.  The compiled graph's ephemeral bundle is deliberately not a
    # review-gate input.
    bundle = validate_frozen_inputs(SessionPaths.from_run_dir(state["data_dir"]))
    _require_verified_inputs(bundle, state=state)
    restored = update_inventory_stage_output(state, {"status": "validated"})
    validated_state = validate_top_level_state({**state, **dict(restored)})
    registration = register_assets(validated_state, verified_inputs=bundle)
    registry_ref = ArtifactRef.model_validate(registration["asset_registry_ref"])
    registered_assets = registration["stage_outputs"]["inventory"].get(
        "registered_assets", []
    )
    if not isinstance(registered_assets, list) or not all(
        isinstance(asset_id, str) for asset_id in registered_assets
    ):
        raise ValueError("inventory registration produced invalid registered_assets")
    registered_asset_ids = [str(asset_id) for asset_id in registered_assets]
    resources = _inventory_resource_effects(registration.get("resource_refs", {}))
    effect = InventoryReviewApplicationEffectV1(
        decision="continue",
        asset_registry_ref=registry_ref,
        registered_asset_ids=tuple(registered_asset_ids),
        resources=resources,
    )
    return PreparedReviewApplication(
        effect_kind="inventory.review_application.v1",
        effect=effect,
        decision_ref=decision_ref,
    )


def apply_inventory_review_application(
    state: TopLevelState,
    application: VerifiedReviewApplication,
) -> StateUpdate:
    """Purely project a verified inventory application into checkpoint state."""

    effect = application.effect
    if not isinstance(effect, InventoryReviewApplicationEffectV1):
        raise TypeError("inventory review application has an unexpected effect")
    if application.application.effect_kind != "inventory.review_application.v1":
        raise ValueError("inventory review application has an unexpected effect_kind")
    _validate_inventory_application_state(state, application)

    if effect.decision == "stop_human_review":
        return {
            **fail_workflow_step(
                state,
                "inventory",
                "inventory review stopped by human decision",
            ),
            "active_interrupt": None,
        }

    if effect.asset_registry_ref is None:
        raise ValueError("inventory continue application is missing asset_registry_ref")
    stage_update = update_inventory_stage_output(
        state,
        {
            "status": "completed",
            "registered_assets": list(effect.registered_asset_ids),
        },
    )
    commit_update = commit_inventory(state)
    return {
        **stage_update,
        **commit_update,
        "asset_registry_ref": effect.asset_registry_ref.model_dump(mode="json"),
        "resource_refs": _resource_refs_from_effect(effect.resources),
        "active_interrupt": None,
    }


def _validate_inventory_intent(state: TopLevelState, intent: ReviewIntentV1) -> None:
    if intent.stage != "inventory" or intent.gate_id != "inventory_review":
        raise ValueError(
            "inventory review application requires inventory/inventory_review"
        )
    if intent.session_id != state["session_id"]:
        raise ValueError("inventory review intent session_id does not match state")
    if intent.workflow_step_id != current_workflow_step_id(
        state, expected_stage="inventory"
    ):
        raise ValueError(
            "inventory review intent workflow_step_id does not match state"
        )


def validate_inventory_review_intent_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    request_record: Mapping[str, Any],
) -> ResumeReviewRequest:
    """Validate one inventory decision before its intent is persisted.

    The coordinator supplies a verified immutable request record.  This helper
    deliberately only reads state and returns the request unchanged so the
    same checks can be repeated immediately before prepare writes artifacts.
    """

    if request.session_id != state["session_id"]:
        raise ResumeValidationError("inventory review request session_id mismatch")
    if request.stage != "inventory" or request.gate_id != "inventory_review":
        raise ResumeValidationError(
            "inventory review requires inventory/inventory_review"
        )
    workflow_step_id = current_workflow_step_id(state, expected_stage="inventory")
    active_interrupt = state.get("active_interrupt")
    if not isinstance(active_interrupt, Mapping):
        raise ResumeValidationError("inventory review requires an active_interrupt")
    try:
        request_ref = ArtifactRef.model_validate(active_interrupt.get("request_ref"))
    except (TypeError, ValueError) as exc:
        raise ResumeValidationError(
            "inventory review active_interrupt has no valid request_ref"
        ) from exc
    if (
        request.review_request_id != active_interrupt.get("review_request_id")
        or request.review_request_hash != active_interrupt.get("request_hash")
        or request.review_request_hash != request_ref.sha256
        or active_interrupt.get("workflow_step_id") != workflow_step_id
        or request_record.get("review_request_id") != request.review_request_id
        or request_record.get("stage") != "inventory"
        or request_record.get("gate_id") != "inventory_review"
        or request_record.get("workflow_step_id") != workflow_step_id
    ):
        raise ResumeValidationError("inventory review request binding mismatch")
    if request.decision not in {"continue", "stop_human_review"}:
        raise ResumeValidationError(
            f"unsupported inventory decision: {request.decision}"
        )
    return request


def _validate_inventory_application_state(
    state: TopLevelState,
    application: VerifiedReviewApplication,
) -> None:
    identity = application.application
    if identity.session_id != state["session_id"]:
        raise ValueError("inventory review application session_id does not match state")
    if identity.stage != "inventory" or identity.gate_id != "inventory_review":
        raise ValueError("inventory review application has an unexpected gate")
    if identity.workflow_step_id != current_workflow_step_id(
        state, expected_stage="inventory"
    ):
        raise ValueError(
            "inventory review application workflow_step_id does not match state"
        )


def _inventory_review_binding(
    state: TopLevelState, intent: ReviewIntentV1
) -> tuple[str, str, ArtifactRef, dict[str, Any]]:
    """Return the verified, immutable request binding for inventory intent."""

    active_interrupt: Mapping[str, Any] = state.get("active_interrupt") or {}
    inventory_out = read_inventory_stage_output(state)
    review_request_id = intent.review_request_id
    review_request_hash = intent.request_hash
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
        or workflow_step_id != intent.workflow_step_id
    ):
        raise ValueError("inventory review request binding mismatch")
    return str(review_request_id), str(review_request_hash), request_ref, request_record


def _inventory_resource_effects(
    resource_refs: object,
) -> tuple[InventoryResourceEffectV1, ...]:
    if not isinstance(resource_refs, Mapping):
        raise ValueError("inventory registration produced invalid resource_refs")
    effects: list[InventoryResourceEffectV1] = []
    for resource_name, raw_ref in sorted(resource_refs.items()):
        if not isinstance(resource_name, str) or not isinstance(raw_ref, Mapping):
            raise ValueError("inventory registration produced invalid resource ref")
        resource = ResourceRef.from_dict(raw_ref)
        effects.append(
            InventoryResourceEffectV1(
                resource_name=resource_name,
                resource_type=resource.resource_type,
                paths=tuple(resource.paths),
                source_stage=resource.source_stage,
                workflow_step_id=resource.workflow_step_id,
                metadata=resource.metadata,
            )
        )
    return tuple(effects)


def _resource_refs_from_effect(
    resources: tuple[InventoryResourceEffectV1, ...],
) -> ResourceRefs:
    return {
        resource.resource_name: ResourceRef(
            resource_type=resource.resource_type,
            paths=list(resource.paths),
            source_stage=resource.source_stage,
            workflow_step_id=resource.workflow_step_id,
            metadata=dict(resource.metadata),
        ).to_dict()
        for resource in resources
    }


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
