"""Verified access to the typed inventory asset registry."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.integrity import sha256_file
from app.schemas.artifacts import ArtifactRef
from app.schemas.assets import AssetRecord, AssetRegistryDocument
from app.schemas.state import TopLevelState
from app.storage.artifacts import resolve_artifact_ref


def load_asset_registry(state: TopLevelState) -> AssetRegistryDocument:
    """Load and validate the current session's authoritative asset registry."""

    raw_ref = state.get("asset_registry_ref")
    if raw_ref is None:
        raise ValueError("asset_registry_ref is missing")
    path = _resolve_registry_path(state, raw_ref)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"asset registry is unreadable: {path}: {exc}") from exc
    try:
        registry = AssetRegistryDocument.model_validate(payload)
    except ValidationError as exc:
        member_id = _invalid_member_id(payload, exc)
        label = f" member {member_id!r}" if member_id else ""
        raise ValueError(f"asset registry{label} is invalid: {exc}") from exc
    if registry.session_id != state["session_id"]:
        raise ValueError(
            "asset registry session_id does not match runtime session: "
            f"{registry.session_id!r} != {state['session_id']!r}"
        )
    return registry


def load_asset_records(
    state: TopLevelState,
    asset_types: set[str] | None = None,
) -> list[AssetRecord]:
    """Return registry records in their immutable document order."""

    records = load_asset_registry(state).assets
    if asset_types is None:
        return list(records)
    return [record for record in records if record.asset_type in asset_types]


def records_by_ordered_ids(
    state: TopLevelState,
    asset_ids: list[str],
    *,
    expected_paths: list[str] | None = None,
) -> list[AssetRecord]:
    """Resolve ordered IDs and verify their one-to-one ResourceRef paths."""

    if len(set(asset_ids)) != len(asset_ids):
        raise ValueError("resource metadata contains duplicate asset IDs")
    by_id = {record.asset_id: record for record in load_asset_registry(state).assets}
    missing = [asset_id for asset_id in asset_ids if asset_id not in by_id]
    if missing:
        raise ValueError(f"resource metadata references missing asset IDs: {missing}")
    records = [by_id[asset_id] for asset_id in asset_ids]
    if expected_paths is not None:
        actual = [
            str(Path(record.path).expanduser().resolve(strict=False))
            for record in records
        ]
        canonical_expected = [
            str(Path(path).expanduser().resolve(strict=False))
            for path in expected_paths
        ]
        if actual != canonical_expected:
            raise ValueError(
                "resource paths do not match ordered registry asset IDs: "
                f"expected={canonical_expected}, actual={actual}"
            )
    for record in records:
        path = Path(record.path)
        if not path.is_file():
            raise ValueError(f"registered asset bytes are missing: {record.asset_id!r}")
        if record.size_bytes is not None and path.stat().st_size != record.size_bytes:
            raise ValueError(f"registered asset size changed: {record.asset_id!r}")
        if (
            record.checksum_status == "ready"
            and record.checksum
            and sha256_file(path) != record.checksum
        ):
            raise ValueError(f"registered asset checksum changed: {record.asset_id!r}")
    return records


def _resolve_registry_path(
    state: TopLevelState,
    raw_ref: ArtifactRef | Mapping[str, Any],
) -> Path:
    if isinstance(raw_ref, str):
        raise ValueError(
            "asset_registry_ref must be an ArtifactRef dict, got bare string: "
            f"{raw_ref!r}"
        )
    if isinstance(raw_ref, ArtifactRef):
        ref = raw_ref
    else:
        ref = ArtifactRef.model_validate(raw_ref)
    return resolve_artifact_ref(
        state.get("data_dir", "data"),
        state["session_id"],
        ref,
    )


def _invalid_member_id(payload: object, error: ValidationError) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return None
    for item in error.errors():
        location = item.get("loc") or ()
        if len(location) < 2 or location[0] != "assets":
            continue
        index = location[1]
        if not isinstance(index, int) or not (0 <= index < len(assets)):
            continue
        member = assets[index]
        if isinstance(member, Mapping) and member.get("asset_id"):
            return str(member["asset_id"])
    return None
