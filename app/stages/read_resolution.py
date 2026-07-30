"""Runtime-only provenance-aware read resolution."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.artifacts import ArtifactRef
from app.schemas.assets import ReadSetManifest
from app.schemas.state import TopLevelState
from app.stages.resource_resolution import resolve_stage_resource
from app.stages.resource_specs import LONG_READS_SPEC
from app.storage.artifacts import resolve_artifact_ref
from app.storage.asset_registry import load_asset_registry, records_by_ordered_ids


class RuntimeReadMember(BaseModel):
    """One read file with its stable source identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    member_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    format: str = Field(min_length=1)
    source_asset_id: str = Field(min_length=1)
    read_artifact_ref: ArtifactRef | None = None
    manifest_entry_ordinal: int | None = Field(default=None, ge=0)
    path_ordinal: int | None = Field(default=None, ge=0)


class RuntimeHiCPair(BaseModel):
    """One verified manifest-owned Hi-C lane pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_entry_ordinal: int = Field(ge=0)
    pair_index: int = Field(ge=0)
    r1: RuntimeReadMember
    r2: RuntimeReadMember


def resolve_long_read_members(
    state: TopLevelState,
    *,
    override_paths: Sequence[str] | None = None,
) -> list[RuntimeReadMember]:
    """Resolve the complete current long-read set without collapsing platform."""

    resolution = resolve_stage_resource(state, resource_spec=LONG_READS_SPEC)
    if not resolution.found:
        return []
    metadata = resolution.metadata
    read_set_ref = metadata.get("read_set_manifest_ref")
    if read_set_ref is not None:
        members = _members_from_read_set(state, read_set_ref, resolution.paths)
    else:
        records = list(resolution.asset_records)
        if not records:
            raise ValueError(
                "registry-backed long-read resource has no typed asset records"
            )
        members = [
            RuntimeReadMember(
                member_id=record.asset_id,
                path=str(record.path),
                platform=str(record.platform or "unknown"),
                format=str(record.format or "unknown"),
                source_asset_id=record.asset_id,
                manifest_entry_ordinal=_metadata_int(
                    record.metadata, "manifest_entry_ordinal"
                ),
                path_ordinal=_metadata_int(record.metadata, "path_ordinal"),
            )
            for record in records
        ]
    return _apply_registered_override(state, members, override_paths)


def resolve_hic_pairs(
    state: TopLevelState,
    *,
    selection_anchor: str | None = None,
) -> list[RuntimeHiCPair]:
    """Reconstruct complete Hi-C pairs from registry metadata."""

    raw_ref = (state.get("resource_refs") or {}).get("hic_reads")
    if not isinstance(raw_ref, Mapping):
        return []
    paths = [str(path) for path in raw_ref.get("paths") or []]
    metadata = raw_ref.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("hic_reads resource metadata must be an object")
    asset_ids = [str(value) for value in metadata.get("asset_ids") or []]
    if len(asset_ids) != len(paths):
        raise ValueError("hic_reads paths and ordered asset IDs differ in cardinality")
    records = records_by_ordered_ids(state, asset_ids, expected_paths=paths)
    grouped: dict[tuple[int, int], dict[str, RuntimeReadMember]] = {}
    selected_entry: int | None = None
    canonical_anchor = (
        str(Path(selection_anchor).expanduser().resolve(strict=False))
        if selection_anchor
        else None
    )
    for record in records:
        entry = _metadata_int(record.metadata, "manifest_entry_ordinal")
        pair_index = _metadata_int(record.metadata, "pair_index")
        mate = record.metadata.get("mate")
        if entry is None or pair_index is None or mate not in {"R1", "R2"}:
            raise ValueError(
                f"Hi-C asset {record.asset_id!r} lacks pair_index/mate provenance"
            )
        if canonical_anchor == str(record.path):
            if selected_entry is not None and selected_entry != entry:
                raise ValueError("hic_reads_ref matches more than one pair group")
            selected_entry = entry
        key = (entry, pair_index)
        roles = grouped.setdefault(key, {})
        if mate in roles:
            raise ValueError(f"duplicate Hi-C {mate} for pair key {key}")
        roles[mate] = RuntimeReadMember(
            member_id=record.asset_id,
            path=str(record.path),
            platform=str(record.platform or "unknown"),
            format=str(record.format or "unknown"),
            source_asset_id=record.asset_id,
            manifest_entry_ordinal=entry,
            path_ordinal=_metadata_int(record.metadata, "path_ordinal"),
        )
    if canonical_anchor is not None and selected_entry is None:
        raise ValueError(
            "assembly.hic_reads_ref is not a member of a registered Hi-C pair group"
        )
    pairs: list[RuntimeHiCPair] = []
    expected_by_entry: dict[int, int] = {}
    for (entry, pair_index), roles in sorted(grouped.items()):
        if selected_entry is not None and entry != selected_entry:
            continue
        expected = expected_by_entry.setdefault(entry, 0)
        if pair_index != expected:
            raise ValueError(
                f"Hi-C pair_index gap for manifest entry {entry}: "
                f"expected {expected}, got {pair_index}"
            )
        expected_by_entry[entry] = expected + 1
        if set(roles) != {"R1", "R2"}:
            raise ValueError(f"incomplete Hi-C pair for key {(entry, pair_index)}")
        pairs.append(
            RuntimeHiCPair(
                manifest_entry_ordinal=entry,
                pair_index=pair_index,
                r1=roles["R1"],
                r2=roles["R2"],
            )
        )
    return pairs


def _members_from_read_set(
    state: TopLevelState,
    raw_ref: object,
    expected_paths: list[str],
) -> list[RuntimeReadMember]:
    ref = ArtifactRef.model_validate(raw_ref)
    path = resolve_artifact_ref(
        state.get("data_dir", "data"),
        state["session_id"],
        ref,
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"read-set manifest is unreadable: {path}: {exc}") from exc
    manifest = ReadSetManifest.model_validate(payload)
    current_registry_ref = ArtifactRef.model_validate(
        state.get("asset_registry_ref")
    )
    if manifest.source_registry_ref != current_registry_ref:
        raise ValueError(
            "read-set manifest source_registry_ref does not match the current "
            "asset registry"
        )
    registry = load_asset_registry(state)
    known_asset_ids = {record.asset_id for record in registry.assets}
    for member in manifest.members:
        if member.source_asset_id not in known_asset_ids:
            raise ValueError(
                f"read-set manifest member references unknown source_asset_id "
                f"{member.source_asset_id!r}"
            )
    if manifest.session_id != state["session_id"]:
        raise ValueError("read-set manifest session does not match runtime session")
    actual_paths = [
        str(
            resolve_artifact_ref(
                state.get("data_dir", "data"),
                state["session_id"],
                member.output_ref,
            )
        )
        for member in manifest.members
    ]
    canonical_expected = [
        str(Path(value).expanduser().resolve(strict=False))
        for value in expected_paths
    ]
    if actual_paths != canonical_expected:
        raise ValueError("read-set members do not match ResourceRef paths")
    return [
        RuntimeReadMember(
            member_id=member.output_ref.artifact_id,
            path=actual_paths[index],
            platform=member.platform,
            format=member.format,
            source_asset_id=member.source_asset_id,
            read_artifact_ref=member.output_ref,
        )
        for index, member in enumerate(manifest.members)
    ]


def _apply_registered_override(
    state: TopLevelState,
    members: list[RuntimeReadMember],
    override_paths: Sequence[str] | None,
) -> list[RuntimeReadMember]:
    if not override_paths:
        return members
    by_path = {
        str(Path(member.path).expanduser().resolve(strict=False)): member
        for member in members
    }
    by_source_asset_id = {
        member.source_asset_id: member
        for member in members
    }
    registry_by_path = {
        str(Path(record.path).expanduser().resolve(strict=False)): record
        for record in load_asset_registry(state).assets
        if record.asset_type in {"raw_long_reads", "clean_long_reads"}
    }
    selected: list[RuntimeReadMember] = []
    for raw_path in override_paths:
        canonical = str(Path(raw_path).expanduser().resolve(strict=False))
        member = by_path.get(canonical)
        if member is None:
            source_record = registry_by_path.get(canonical)
            if source_record is not None:
                member = by_source_asset_id.get(source_record.asset_id)
        if member is None:
            raise ValueError(f"unregistered_long_read_override: {raw_path}")
        if member in selected:
            raise ValueError(f"duplicate long-read override: {raw_path}")
        selected.append(member)
    return selected


def _metadata_int(metadata: Mapping[str, object], key: str) -> int | None:
    value = metadata.get(key)
    return value if isinstance(value, int) and value >= 0 else None
