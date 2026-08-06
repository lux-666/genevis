import json
from pathlib import Path

import pytest

from app.schemas.artifacts import ArtifactMeta
from app.schemas.limits import RESOURCE_REFS_MAX_BYTES
from app.schemas.resource_ref import ResourceRef
from app.schemas.state import initial_state, validate_resource_refs
from app.stages.resource_resolution import (
    ResourceSpec,
    resolve_stage_resource,
    update_resource_ref,
)
from app.storage.artifacts import publish_artifact_record
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


def _publish_registry_ref(data_dir: str, session_id: str, registry_path: Path) -> dict:
    paths = SessionPaths.from_run_dir(data_dir)
    source = paths.workflow_step_work_path(
        "step-000-inventory", "inventory", "asset_registry.json"
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(registry_path.read_text(encoding="utf-8"), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        source,
        stage="inventory",
        method_name="inventory.register_assets",
        artifact_type="asset_registry",
    )
    published = publish_artifact_record(data_dir, session_id, artifact)
    return published.ref.model_dump(mode="json")


def test_resolution_precedence_is_central_then_registry(tmp_path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "s1",
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
    state["data_dir"] = str(v2_run_dir(tmp_path, "s1"))
    registry_ref = _publish_registry_ref(state["data_dir"], "s1", registry_path)
    state["asset_registry_ref"] = registry_ref
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

    assert central.paths == ["/central/clean.fastq.gz"]
    assert central.source == "read_qc"
    assert central.metadata == {"quality": "filtered"}


def test_registry_fallback_honors_asset_type_priority_and_multiple(tmp_path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "s1",
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
    state["data_dir"] = str(v2_run_dir(tmp_path, "s1"))
    registry_ref = _publish_registry_ref(state["data_dir"], "s1", registry_path)
    state["asset_registry_ref"] = registry_ref

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


def test_inventory_resource_ref_honors_asset_type_priority(tmp_path) -> None:
    state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
    raw_path = tmp_path / "raw" / "one.fastq.gz"
    clean_path = tmp_path / "clean" / "one.fastq.gz"
    raw_path.parent.mkdir()
    clean_path.parent.mkdir()
    raw_path.write_bytes(b"raw")
    clean_path.write_bytes(b"clean")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "s1",
                "assets": [
                    {
                        "asset_id": "raw-1",
                        "asset_type": "raw_long_reads",
                        "path": str(raw_path),
                    },
                    {
                        "asset_id": "clean-1",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_path),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    state["data_dir"] = str(v2_run_dir(tmp_path, "s1"))
    registry_ref = _publish_registry_ref(state["data_dir"], "s1", registry_path)
    state["asset_registry_ref"] = registry_ref
    state["resource_refs"]["long_reads"] = ResourceRef(
        resource_type="long_reads",
        paths=[str(raw_path), str(clean_path)],
        source_stage=None,
        metadata={
            "asset_registry_ref": registry_ref,
            "asset_ids": ["raw-1", "clean-1"],
            "member_count": 2,
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

    assert resolved.paths == [str(clean_path)]
    assert resolved.metadata["asset_ids"] == ["clean-1"]


def test_update_resource_ref_returns_validated_replacement_without_mutation() -> None:
    state = initial_state(
        session_id="s1",
        input_manifest_ref="manifest.yaml",
        workflow_steps=[
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "step_id": "step-001-assembly",
                "stage": "assembly",
                "allow_local_execution": False,
            },
        ],
    )
    state["current_stage"] = "assembly"
    state["workflow_step_index"] = 1
    state["entered_workflow_step_index"] = 1
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
    assert replacement["assembly"]["workflow_step_id"] == "step-001-assembly"


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
