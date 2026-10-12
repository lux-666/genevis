"""Runtime-only provenance-aware read resolution."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.artifacts import ArtifactRef
from app.schemas.assets import AssetRecord, ReadSetManifest
from app.schemas.manifest import Compression
from app.schemas.state import TopLevelState
from app.stages.resource_resolution import ResourceSpec, resolve_stage_resource
from app.stages.resource_specs import LONG_READS_SPEC, SHORT_READS_SPEC
from app.storage.artifacts import resolve_artifact_ref
from app.storage.asset_registry import load_asset_registry, records_by_ordered_ids


class RuntimeReadMember(BaseModel):
    """One read file with its stable source identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    member_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    format: str = Field(min_length=1)
    compression: Compression = "unknown"
    source_asset_id: str = Field(min_length=1)
    read_artifact_ref: ArtifactRef | None = None
    manifest_entry_ordinal: int | None = Field(default=None, ge=0)
    pair_index: int | None = Field(default=None, ge=0)
    mate: str | None = None
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

    return _resolve_read_members(state, LONG_READS_SPEC, override_paths)


def resolve_short_read_members(state: TopLevelState) -> list[RuntimeReadMember]:
    """Resolve child short reads, retaining every QC artifact and compression fact."""
    return _resolve_read_members(state, SHORT_READS_SPEC, None)


def _resolve_read_members(
    state: TopLevelState,
    resource_spec: ResourceSpec,
    override_paths: Sequence[str] | None,
) -> list[RuntimeReadMember]:
    resolution = resolve_stage_resource(state, resource_spec=resource_spec)
    if not resolution.found:
        return []
    metadata = resolution.metadata
    read_set_ref = metadata.get("read_set_manifest_ref")
    if read_set_ref is not None:
        members = _members_from_read_set(state, read_set_ref, resolution.paths)
    else:
        records = list(resolution.asset_records)
        if not records:
            raise ValueError("registry-backed read resource has no typed asset records")
        members = [
            RuntimeReadMember(
                member_id=record.asset_id,
                path=str(record.path),
                platform=str(record.platform or "unknown"),
                format=str(record.format or "unknown"),
                compression=cast(
                    Compression, str(record.metadata.get("compression") or "unknown")
                ),
                source_asset_id=record.asset_id,
                manifest_entry_ordinal=_metadata_int(
                    record.metadata, "manifest_entry_ordinal"
                ),
                path_ordinal=_metadata_int(record.metadata, "path_ordinal"),
            )
            for record in records
        ]
    return _apply_registered_override(state, members, override_paths)


def resolve_trio_parent_members(
    state: TopLevelState,
    *,
    override_paths: Sequence[str] | None = None,
) -> list[RuntimeReadMember]:
    """Resolve registered parental reads for Trio binning.

    Trio parental material is not the assembly long-read resource.  In the
    native hifiasm workflow it is counted by ``yak`` first, so parental FASTQ
    may be short-read material and must never be forced through the primary
    HiFi/ONT resolver.
    """

    from app.storage.asset_registry import load_asset_records

    records = load_asset_records(
        state,
        {
            "raw_short_dna_reads",
            "clean_short_dna_reads",
            "raw_long_reads",
            "clean_long_reads",
        },
    )
    members = [
        RuntimeReadMember(
            member_id=record.asset_id,
            path=str(record.path),
            platform=str(record.platform or "unknown"),
            format=str(record.format or "unknown"),
            compression=cast(
                Compression, str(record.metadata.get("compression") or "unknown")
            ),
            source_asset_id=record.asset_id,
            manifest_entry_ordinal=_metadata_int(
                record.metadata, "manifest_entry_ordinal"
            ),
            path_ordinal=_metadata_int(record.metadata, "path_ordinal"),
        )
        for record in records
        if record.metadata.get("intake_role")
        in {"trio_maternal_reads", "trio_paternal_reads"}
    ]
    return _apply_registered_override(state, members, override_paths)


def resolve_hic_pairs(
    state: TopLevelState,
    *,
    selection_anchor: str | None = None,
) -> list[RuntimeHiCPair]:
    """Reconstruct complete Hi-C pairs from the current resource.

    A QC-produced resource is an immutable derived read set.  Its ordered
    ``paired_members`` metadata is authoritative for the current paths; the
    ``source_asset_id`` on each member is only the lineage anchor back to the
    inventory registry.  Do not replace those clean paths with registry paths
    when a clean resource is selected.
    """

    raw_ref = (state.get("resource_refs") or {}).get("hic_reads")
    if not isinstance(raw_ref, Mapping):
        return []
    raw_paths = raw_ref.get("paths")
    if not isinstance(raw_paths, list):
        raise ValueError("hic_reads resource paths must be an array")
    paths = [str(path) for path in raw_paths]
    metadata = raw_ref.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("hic_reads resource metadata must be an object")
    read_set_ref = metadata.get("read_set_manifest_ref")
    if read_set_ref is not None:
        return _resolve_hic_pairs_from_read_set(
            state,
            raw_ref,
            read_set_ref,
            metadata.get("paired_members"),
            selection_anchor=selection_anchor,
        )
    derived = metadata.get("paired_members")
    if derived is not None and not (
        isinstance(metadata.get("asset_ids"), list)
        and metadata.get("asset_registry_ref") is not None
    ):
        raise ValueError(
            "derived hic_reads paired_members require read_set_manifest_ref"
        )
    raw_asset_ids = metadata.get("asset_ids")
    if not isinstance(raw_asset_ids, list):
        raise ValueError("hic_reads resource asset_ids must be an array")
    asset_ids = [str(value) for value in raw_asset_ids]
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
            compression=cast(
                Compression, str(record.metadata.get("compression") or "unknown")
            ),
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


def _resolve_hic_pairs_from_read_set(
    state: TopLevelState,
    raw_ref: Mapping[str, object],
    read_set_ref: object,
    raw_pairs: object,
    *,
    selection_anchor: str | None,
) -> list[RuntimeHiCPair]:
    """Resolve Hi-C pairs from the verified clean read-set manifest."""

    raw_paths = raw_ref.get("paths")
    if not isinstance(raw_paths, list):
        raise ValueError("hic_reads derived resource paths must be an array")
    expected_paths = [str(path) for path in raw_paths]
    verified_members = _members_from_read_set(state, read_set_ref, expected_paths)
    registry = load_asset_registry(state)
    records_by_id = {record.asset_id: record for record in registry.assets}
    grouped: dict[tuple[int, int], dict[str, RuntimeReadMember]] = {}
    expected_declarations: list[dict[str, object]] = []
    selected_entry: int | None = None
    canonical_anchor = (
        str(Path(selection_anchor).expanduser().resolve(strict=False))
        if selection_anchor
        else None
    )
    for verified_member in verified_members:
        entry = verified_member.manifest_entry_ordinal
        pair_index = verified_member.pair_index
        mate = verified_member.mate
        if entry is None or pair_index is None or mate not in {"R1", "R2"}:
            raise ValueError("Hi-C read-set member lacks pair_index/mate provenance")
        key = (entry, pair_index)
        source_record = records_by_id.get(verified_member.source_asset_id)
        if source_record is None:
            raise ValueError(
                f"derived Hi-C member references unknown source asset "
                f"{verified_member.source_asset_id!r}"
            )
        _validate_hic_source_record(source_record, entry, pair_index, mate, key)
        if mate in grouped.setdefault(key, {}):
            raise ValueError(f"duplicate derived Hi-C {mate} for pair key {key}")
        if verified_member.read_artifact_ref is None:
            raise ValueError(f"derived Hi-C member {key}/{mate} has no ArtifactRef")
        if canonical_anchor in {
            verified_member.path,
            str(Path(source_record.path).expanduser().resolve(strict=False)),
            verified_member.source_asset_id,
        }:
            if selected_entry is not None and selected_entry != entry:
                raise ValueError("hic_reads_ref matches more than one pair group")
            selected_entry = entry
        grouped[key][mate] = RuntimeReadMember(
            **verified_member.model_dump(
                exclude={"manifest_entry_ordinal", "path_ordinal"}
            ),
            manifest_entry_ordinal=entry,
            path_ordinal=None,
        )
        expected_declarations.append(
            {
                "manifest_entry_ordinal": entry,
                "pair_index": pair_index,
                "mate": mate,
                "source_asset_id": verified_member.source_asset_id,
                "path": verified_member.path,
                "artifact_ref": verified_member.read_artifact_ref,
            }
        )
    if raw_pairs is not None:
        _validate_hic_paired_member_declarations(raw_pairs, expected_declarations)
    if canonical_anchor is not None and selected_entry is None:
        raise ValueError(
            "assembly.hic_reads_ref is not a member or source of a registered "
            "Hi-C pair group"
        )
    return _finish_hic_pairs(grouped, selected_entry)


def _validate_hic_source_record(
    record: AssetRecord,
    entry: int,
    pair_index: int,
    mate: str,
    key: tuple[int, int],
) -> None:
    """Require a clean member's source to be a matching registered Hi-C asset."""

    if record.asset_type != "hic_reads_registered":
        raise ValueError(
            f"derived Hi-C member {key}/{mate} source asset type is "
            f"{record.asset_type!r}, expected 'hic_reads_registered'"
        )
    if (
        record.metadata.get("manifest_entry_ordinal") != entry
        or record.metadata.get("pair_index") != pair_index
        or record.metadata.get("mate") != mate
    ):
        raise ValueError(
            f"derived Hi-C member {key}/{mate} source registry identity does not "
            "match the clean declaration"
        )


def _validate_hic_paired_member_declarations(
    raw_pairs: object,
    expected: list[dict[str, object]],
) -> None:
    """Cross-check ResourceRef paired_members against its verified manifest."""

    if not isinstance(raw_pairs, list):
        raise ValueError("hic_reads paired_members must be an array")
    actual: list[dict[str, object]] = []
    for index, raw_pair in enumerate(raw_pairs):
        if not isinstance(raw_pair, Mapping):
            raise ValueError(f"hic_reads paired_members[{index}] must be an object")
        entry = raw_pair.get("manifest_entry_ordinal")
        pair_index = raw_pair.get("pair_index")
        for mate in ("R1", "R2"):
            member = raw_pair.get(mate.lower())
            if not isinstance(member, Mapping):
                raise ValueError(f"hic_reads paired_members[{index}] is missing {mate}")
            artifact_ref = member.get("artifact_ref")
            if artifact_ref is None:
                raise ValueError(
                    f"hic_reads paired_members[{index}]/{mate} has no ArtifactRef"
                )
            actual.append(
                {
                    "manifest_entry_ordinal": entry,
                    "pair_index": pair_index,
                    "mate": member.get("mate"),
                    "source_asset_id": member.get("source_asset_id"),
                    "path": str(
                        Path(str(member.get("path"))).expanduser().resolve(strict=False)
                    ),
                    "artifact_ref": ArtifactRef.model_validate(artifact_ref),
                }
            )
    if actual != expected:
        raise ValueError(
            "Hi-C paired_members do not match the verified read-set manifest"
        )


def _finish_hic_pairs(
    grouped: dict[tuple[int, int], dict[str, RuntimeReadMember]],
    selected_entry: int | None,
) -> list[RuntimeHiCPair]:
    """Apply strict pair completeness and contiguous lane ordering."""

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
    current_registry_ref = ArtifactRef.model_validate(state.get("asset_registry_ref"))
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
        str(Path(value).expanduser().resolve(strict=False)) for value in expected_paths
    ]
    if actual_paths != canonical_expected:
        raise ValueError("read-set members do not match ResourceRef paths")
    return [
        RuntimeReadMember(
            member_id=member.output_ref.artifact_id,
            path=actual_paths[index],
            platform=member.platform,
            format=member.format,
            compression=member.compression,
            source_asset_id=member.source_asset_id,
            read_artifact_ref=member.output_ref,
            manifest_entry_ordinal=member.manifest_entry_ordinal,
            pair_index=member.pair_index,
            mate=member.mate,
            path_ordinal=None,
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
    by_source_asset_id = {member.source_asset_id: member for member in members}
    registry_by_path = {
        str(Path(record.path).expanduser().resolve(strict=False)): record
        for record in load_asset_registry(state).assets
        if record.asset_type
        in {
            "raw_long_reads",
            "clean_long_reads",
            "raw_short_dna_reads",
            "clean_short_dna_reads",
        }
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
