import json

import pytest

from app.schemas.limits import RESOURCE_REFS_MAX_BYTES
from app.schemas.resource_ref import ResourceRef
from app.schemas.state import initial_state, validate_resource_refs
from app.stages.resource_resolution import (
    ResourceSpec,
    resolve_stage_resource,
    update_resource_ref,
)


def test_resolution_precedence_is_override_then_central_then_registry(tmp_path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "asset_id": "raw-1",
                        "asset_type": "raw_long_reads",
                        "path": "/registry/raw.fastq.gz",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
    state["asset_registry_ref"] = str(registry_path)
    state["resource_refs"]["long_reads"] = ResourceRef(
        resource_type="long_reads",
        paths=["/central/clean.fastq.gz"],
        source_stage="read_qc",
        metadata={"quality": "filtered"},
    ).to_dict()
    spec = ResourceSpec(
        resource_type="long_reads",
        asset_types=["raw_long_reads"],
        allow_multiple=True,
    )

    central = resolve_stage_resource(state, resource_spec=spec)
    override = resolve_stage_resource(
        state, resource_spec=spec, override="/override/reads.fastq.gz"
    )

    assert central.paths == ["/central/clean.fastq.gz"]
    assert central.source == "read_qc"
    assert central.metadata == {"quality": "filtered"}
    assert override.paths == ["/override/reads.fastq.gz"]
    assert override.source_type == "override"


def test_registry_fallback_honors_asset_type_priority_and_multiple(tmp_path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "asset_id": "raw-1",
                        "asset_type": "raw_long_reads",
                        "path": "/raw/one.fastq.gz",
                    },
                    {
                        "asset_id": "clean-1",
                        "asset_type": "clean_long_reads",
                        "path": "/clean/one.fastq.gz",
                    },
                    {
                        "asset_id": "clean-2",
                        "asset_type": "clean_long_reads",
                        "path": "/clean/two.fastq.gz",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
    state["asset_registry_ref"] = str(registry_path)

    resolved = resolve_stage_resource(
        state,
        resource_spec=ResourceSpec(
            resource_type="long_reads",
            asset_types=["clean_long_reads", "raw_long_reads"],
            allow_multiple=True,
        ),
    )

    assert resolved.paths == ["/clean/one.fastq.gz", "/clean/two.fastq.gz"]
    assert resolved.metadata["asset_ids"] == ["clean-1", "clean-2"]
    assert resolved.metadata["asset_types"] == ["clean_long_reads"]


def test_inventory_resource_ref_honors_asset_type_priority() -> None:
    state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
    state["resource_refs"]["long_reads"] = ResourceRef(
        resource_type="long_reads",
        paths=["/raw/one.fastq.gz", "/clean/one.fastq.gz"],
        source_stage=None,
        metadata={
            "assets": [
                {
                    "asset_type": "raw_long_reads",
                    "path": "/raw/one.fastq.gz",
                },
                {
                    "asset_type": "clean_long_reads",
                    "path": "/clean/one.fastq.gz",
                },
            ]
        },
    ).to_dict()

    resolved = resolve_stage_resource(
        state,
        resource_spec=ResourceSpec(
            resource_type="long_reads",
            asset_types=["clean_long_reads", "raw_long_reads"],
            allow_multiple=True,
        ),
    )

    assert resolved.paths == ["/clean/one.fastq.gz"]


def test_update_resource_ref_returns_validated_replacement_without_mutation() -> None:
    state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
    original = dict(state["resource_refs"])

    replacement = update_resource_ref(
        state,
        "assembly",
        ["/work/a.fasta", "/work/a.fasta"],
        "assembly",
        {"candidate_id": "a"},
    )

    assert state["resource_refs"] == original
    assert replacement["assembly"]["paths"] == ["/work/a.fasta"]
    assert replacement["assembly"]["source_stage"] == "assembly"


def test_resource_table_rejects_invalid_and_oversized_entries() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ResourceRef(resource_type="assembly", paths=[])
    with pytest.raises(ValueError, match="must match"):
        validate_resource_refs(
            {
                "assembly": ResourceRef(
                    resource_type="long_reads", paths=["/reads.fastq.gz"]
                ).to_dict()
            }
        )
    with pytest.raises(ValueError, match="unknown fields"):
        validate_resource_refs(
            {
                "assembly": {
                    **ResourceRef(
                        resource_type="assembly", paths=["/assembly.fasta"]
                    ).to_dict(),
                    "legacy_output_key": "draft_assembly_ref",
                }
            }
        )
    with pytest.raises(TypeError, match="non-JSON value"):
        validate_resource_refs(
            {
                "assembly": ResourceRef(
                    resource_type="assembly",
                    paths=["/assembly.fasta"],
                    metadata={"bad": object()},
                ).to_dict()
            }
        )
    with pytest.raises(ValueError, match="exceeds"):
        validate_resource_refs(
            {
                "assembly": ResourceRef(
                    resource_type="assembly",
                    paths=["/assembly.fasta"],
                    metadata={"payload": "x" * RESOURCE_REFS_MAX_BYTES},
                ).to_dict()
            }
        )
