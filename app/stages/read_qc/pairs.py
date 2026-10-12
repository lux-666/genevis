"""Canonical paired-read resolution for short-DNA and Hi-C QC."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, cast

from app.schemas.artifacts import ArtifactRef
from app.schemas.state import TopLevelState
from app.stages.read_resolution import _members_from_read_set, resolve_hic_pairs
from app.stages.resource_resolution import resolve_stage_resource
from app.stages.resource_specs import HIC_READS_SPEC, SHORT_READS_SPEC


def resolve_qc_pairs(state: TopLevelState, family: str) -> list[dict[str, Any]]:
    """Resolve explicit R1/R2 lane pairs without filename inference."""

    if family == "hic_reads":
        pairs = resolve_hic_pairs(state)
        if pairs:
            return _validate_pairs([_hic_pair_payload(pair) for pair in pairs], family)
        resolution = resolve_stage_resource(state, resource_spec=HIC_READS_SPEC)
        if not resolution.found:
            return []
        return _pairs_from_records(list(resolution.asset_records), family)
    if family != "short_reads":
        raise ValueError(f"unsupported paired-read QC family: {family}")
    resolution = resolve_stage_resource(state, resource_spec=SHORT_READS_SPEC)
    if not resolution.found:
        return []
    metadata = resolution.metadata
    if metadata.get("read_set_manifest_ref") is not None:
        derived = metadata.get("paired_members")
        if not isinstance(derived, list):
            raise ValueError(
                "derived short_reads resource requires paired_members array"
            )
        return _validate_derived_pairs_against_read_set(
            state, resolution.paths, metadata["read_set_manifest_ref"], derived, family
        )
    if "paired_members" in metadata and not (
        metadata.get("asset_registry_ref") is not None
        and isinstance(metadata.get("asset_ids"), list)
    ):
        raise ValueError(
            "derived short_reads paired_members require read_set_manifest_ref"
        )
    records = list(resolution.asset_records)
    if not records:
        raise ValueError("short_reads resource has no registry-backed asset records")
    return _pairs_from_records(records, family)


def _validate_pairs(
    raw_pairs: list[dict[str, Any]], family: str
) -> list[dict[str, Any]]:
    """Validate the frozen lane/mate identity before a command is emitted."""

    validated: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for pair in raw_pairs:
        if not isinstance(pair, dict):
            raise ValueError(f"{family} pair record must be an object")
        entry = pair.get("manifest_entry_ordinal")
        pair_index = pair.get("pair_index")
        if (
            type(entry) is not int
            or entry < 0
            or type(pair_index) is not int
            or pair_index < 0
        ):
            raise ValueError(f"{family} pair has invalid manifest identity")
        identity = (entry, pair_index)
        if identity in seen:
            raise ValueError(f"duplicate {family} pair identity {identity}")
        seen.add(identity)
        members: dict[str, dict[str, Any]] = {}
        for expected_mate in ("R1", "R2"):
            raw_member = pair.get(expected_mate.lower())
            if not isinstance(raw_member, dict):
                raise ValueError(f"{family} pair {identity} is missing {expected_mate}")
            member = cast(dict[str, Any], raw_member)
            if member.get("mate") != expected_mate:
                raise ValueError(
                    f"{family} pair {identity} has {expected_mate} member with "
                    f"mate={member.get('mate')!r}"
                )
            source_id = member.get("source_asset_id") or member.get("member_id")
            path = member.get("path")
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError(
                    f"{family} pair {identity} has no source member identity"
                )
            if not isinstance(path, str) or not path.strip():
                raise ValueError(f"{family} pair {identity} has no member path")
            if (
                member.get("manifest_entry_ordinal") != entry
                or member.get("pair_index") != pair_index
            ):
                raise ValueError(
                    f"{family} pair {identity} member identity does not match pair"
                )
            members[expected_mate] = member
        if members["R1"].get("source_asset_id") == members["R2"].get("source_asset_id"):
            raise ValueError(
                f"{family} pair {identity} reuses one source member for R1/R2"
            )
        validated.append({**pair, "r1": members["R1"], "r2": members["R2"]})
    return validated


def _validate_derived_pairs_against_read_set(
    state: TopLevelState,
    paths: list[str],
    read_set_ref: object,
    raw_pairs: list[object],
    family: str,
) -> list[dict[str, Any]]:
    """Validate mutable pair metadata against the immutable read-set manifest."""

    for index, pair in enumerate(raw_pairs):
        if not isinstance(pair, dict):
            raise ValueError(
                f"{family} paired_members[{index}] must be an object; "
                "refusing to silently drop a lane"
            )
    validated = _validate_pairs(cast(list[dict[str, Any]], raw_pairs), family)
    members = _members_from_read_set(state, read_set_ref, paths)
    grouped: dict[tuple[int, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for member in members:
        entry = member.manifest_entry_ordinal
        pair_index = member.pair_index
        mate = member.mate
        if entry is None or pair_index is None or mate is None:
            raise ValueError(
                f"{family} read-set member lacks pair_index/mate provenance"
            )
        key = (entry, pair_index)
        if mate in grouped[key]:
            raise ValueError(f"duplicate {family} {mate} for pair key {key}")
        if member.read_artifact_ref is None:
            raise ValueError(f"{family} read-set member has no ArtifactRef")
        grouped[key][mate] = {
            "path": member.path,
            "platform": member.platform,
            "format": member.format,
            "compression": member.compression,
            "source_asset_id": member.source_asset_id,
            "artifact_ref": member.read_artifact_ref.model_dump(mode="json"),
            "manifest_entry_ordinal": entry,
            "pair_index": pair_index,
            "mate": mate,
        }
    expected = _pairs_from_verified_members(grouped, family)
    if len(validated) != len(expected):
        raise ValueError(f"{family} paired_members do not match read-set manifest")
    for actual, wanted in zip(validated, expected, strict=True):
        for mate in ("r1", "r2"):
            actual_member = actual[mate]
            expected_member = wanted[mate]
            for field_name in (
                "path",
                "platform",
                "format",
                "compression",
                "source_asset_id",
                "manifest_entry_ordinal",
                "pair_index",
                "mate",
            ):
                if actual_member.get(field_name) != expected_member.get(field_name):
                    raise ValueError(
                        f"{family} paired_members do not match read-set manifest"
                    )
            actual_ref = ArtifactRef.model_validate(actual_member.get("artifact_ref"))
            expected_ref = ArtifactRef.model_validate(expected_member["artifact_ref"])
            if actual_ref != expected_ref:
                raise ValueError(
                    f"{family} paired_members do not match read-set manifest"
                )
    return validated


def _pairs_from_verified_members(
    grouped: dict[tuple[int, int], dict[str, dict[str, Any]]], family: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    expected_by_entry: dict[int, int] = {}
    for (entry, pair_index), members in sorted(grouped.items()):
        expected = expected_by_entry.setdefault(entry, 0)
        if pair_index != expected:
            raise ValueError(
                f"{family} pair_index gap for manifest entry {entry}: "
                f"expected {expected}, got {pair_index}"
            )
        expected_by_entry[entry] = expected + 1
        if set(members) != {"R1", "R2"}:
            raise ValueError(f"incomplete {family} pair for key {(entry, pair_index)}")
        result.append(
            {
                "pair_id": f"{family}:{entry}:{pair_index}",
                "manifest_entry_ordinal": entry,
                "pair_index": pair_index,
                "r1": members["R1"],
                "r2": members["R2"],
            }
        )
    return result


def _hic_pair_payload(pair: Any) -> dict[str, Any]:
    def member_payload(member: Any, mate: str) -> dict[str, Any]:
        payload = member.model_dump(mode="json")
        return {
            key: payload[key]
            for key in (
                "member_id",
                "path",
                "platform",
                "format",
                "compression",
                "source_asset_id",
                "manifest_entry_ordinal",
            )
            if payload.get(key) is not None
        } | {
            "manifest_entry_ordinal": pair.manifest_entry_ordinal,
            "pair_index": pair.pair_index,
            "mate": mate,
            **(
                {
                    "artifact_ref": payload["read_artifact_ref"],
                }
                if payload.get("read_artifact_ref") is not None
                else {}
            ),
        }

    return {
        "pair_id": f"hic_reads:{pair.manifest_entry_ordinal}:{pair.pair_index}",
        "manifest_entry_ordinal": pair.manifest_entry_ordinal,
        "pair_index": pair.pair_index,
        "r1": member_payload(pair.r1, "R1"),
        "r2": member_payload(pair.r2, "R2"),
    }


def _member_payload(record: Any) -> dict[str, Any]:
    return {
        "member_id": record.asset_id,
        "path": str(record.path),
        "platform": str(record.platform or "unknown"),
        "format": str(record.format or "unknown"),
        "source_asset_id": record.asset_id,
        "manifest_entry_ordinal": _metadata_int(
            record.metadata, "manifest_entry_ordinal"
        ),
        "pair_index": _metadata_int(record.metadata, "pair_index"),
        "mate": record.metadata.get("mate"),
    }


def _pairs_from_records(records: list[Any], family: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for record in records:
        entry = _metadata_int(record.metadata, "manifest_entry_ordinal")
        pair_index = _metadata_int(record.metadata, "pair_index")
        mate = record.metadata.get("mate")
        if entry is None or pair_index is None or mate not in {"R1", "R2"}:
            raise ValueError(
                f"{family} asset {record.asset_id!r} lacks pair_index/mate provenance"
            )
        key = (entry, pair_index)
        if mate in grouped[key]:
            raise ValueError(f"duplicate {family} {mate} for pair key {key}")
        grouped[key][mate] = _member_payload(record)
    return _pairs_from_verified_members(grouped, family)


def _metadata_int(metadata: dict[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    return value if type(value) is int and value >= 0 else None
