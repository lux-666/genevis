"""Import completed QC evidence and rebind its read sets to a new session."""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing
from errno import EXDEV
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from app.control.workflow_plan import current_workflow_step_id
from app.schemas.artifacts import ArtifactMeta, ArtifactRef
from app.schemas.assets import AssetRecord, ReadSetManifest
from app.schemas.run_config import ReadQCReuseSource
from app.schemas.state import StateUpdate, TopLevelState, validate_top_level_state
from app.stages.read_qc.state import (
    read_read_qc_stage_output,
    replace_read_qc_stage_output,
)
from app.stages.resource_specs import HIC_READS_SPEC, LONG_READS_SPEC, SHORT_READS_SPEC
from app.storage.artifacts import (
    PublishedArtifact,
    read_artifact_index_view,
    record_registered_artifacts,
    register_artifact_records,
)
from app.storage.asset_registry import load_asset_records
from app.storage.ledger import Ledger, project_stage_terminal_events
from app.storage.paths import SessionPaths
from app.storage.publish import publish_file


def read_completed_qc_source(paths: SessionPaths) -> tuple[TopLevelState, str]:
    """Verify the latest entered QC occurrence against the checkpoint and Ledger."""
    if not paths.checkpoint_db_path.is_file():
        raise ValueError(
            f"source session {paths.session_id!r} has no SQLite checkpoint"
        )
    try:
        with closing(
            sqlite3.connect(f"{paths.checkpoint_db_path.as_uri()}?mode=ro", uri=True)
        ) as connection:
            saver = SqliteSaver(connection)
            saver.is_setup = True  # The source database must already be initialized.
            checkpoint = saver.get_tuple(
                {"configurable": {"thread_id": paths.session_id}}
            )
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read source QC checkpoint: {exc}") from exc
    if checkpoint is None:
        raise ValueError(f"source session {paths.session_id!r} has no checkpoint state")
    state = validate_top_level_state(checkpoint.checkpoint["channel_values"])
    if (
        state["session_id"] != paths.session_id
        or Path(state["data_dir"]) != paths.run_dir
    ):
        raise ValueError("source QC checkpoint does not belong to the requested run")

    entered = state.get("entered_workflow_step_index", -1)
    steps = state["workflow_steps"]
    step_id = next(
        (
            step["step_id"]
            for step in reversed(steps[: entered + 1])
            if step["stage"] == "read_qc"
        ),
        None,
    )
    terminals, conflicts = project_stage_terminal_events(
        Ledger(paths.run_dir).read_event_rows(paths.session_id)
    )
    if (
        step_id is None
        or terminals.get((step_id, "read_qc"), {}).get("event_type")
        != "stage_completed"
        or any(conflict["workflow_step_id"] == step_id for conflict in conflicts)
        or read_read_qc_stage_output(state).get("status") != "completed"
    ):
        raise ValueError(
            f"source session {paths.session_id!r} has no completed QC to reuse"
        )
    return state, step_id


def import_completed_read_qc(state: TopLevelState) -> StateUpdate:
    """Prepare normal QC handoff with child-local refs, without starting QC tools."""
    try:
        return _import_completed_read_qc(state)
    except (OSError, ValueError) as exc:
        return replace_read_qc_stage_output(
            state, {"status": "failed", "reason": f"cannot reuse read QC: {exc}"}
        )


def _import_completed_read_qc(state: TopLevelState) -> StateUpdate:
    source = ReadQCReuseSource.model_validate(
        read_read_qc_stage_output(state).get("reuse")
    )
    target_paths = SessionPaths.from_run_dir(state["data_dir"])
    source_paths = SessionPaths(target_paths.data_root, source.session_id)
    if source.session_id == target_paths.session_id:
        raise ValueError("the QC source must be a different session")
    source_state, source_step = read_completed_qc_source(source_paths)
    if source_step != source.workflow_step_id:
        raise ValueError(
            f"source QC cursor no longer belongs to {source.workflow_step_id}"
        )
    source_registry_ref = ArtifactRef.model_validate(source_state["asset_registry_ref"])
    target_registry_ref = ArtifactRef.model_validate(state["asset_registry_ref"])
    asset_ids = _match_read_assets(source_state, state)
    target_step = current_workflow_step_id(state, expected_stage="read_qc")
    view = read_artifact_index_view(source_paths.run_dir, source_paths.session_id)
    imported: dict[str, PublishedArtifact] = {}
    work = target_paths.workflow_step_work_path(target_step, "read_qc", "reuse")
    work.mkdir(parents=True, exist_ok=True)

    def import_artifact(ref: ArtifactRef) -> PublishedArtifact:
        record = view.record_for_ref(ref)
        if record["workflow_step_id"] != source_step or record["stage"] != "read_qc":
            raise ValueError(
                f"artifact is outside the source QC occurrence: {ref.path_ref}"
            )
        if ref.artifact_id in imported:
            return imported[ref.artifact_id]
        is_read_set = ref.artifact_type.endswith("read_set_manifest")
        path = (
            view.resolve(ref) if is_read_set else source_paths.resolve_ref(ref.path_ref)
        )
        destination = target_paths.workflow_step_artifact_path(
            target_step, "read_qc", "reused", ref.artifact_id, path.name
        )
        metadata: dict[str, Any] = {
            "provenance": {
                "source_session_id": source.session_id,
                "source_workflow_step_id": source_step,
                "source_artifact_ref": ref.model_dump(mode="json"),
            }
        }
        with TemporaryDirectory(dir=work) as temporary:
            staged = Path(temporary) / path.name
            if is_read_set:
                manifest = ReadSetManifest.model_validate_json(path.read_bytes())
                if (
                    manifest.session_id != source.session_id
                    or manifest.source_registry_ref != source_registry_ref
                ):
                    raise ValueError(
                        f"read-set manifest belongs to other inputs: {ref.path_ref}"
                    )
                members = []
                for member in manifest.members:
                    asset_id = asset_ids.get(member.source_asset_id)
                    if asset_id is None or member.output_ref.artifact_type.endswith(
                        "read_set_manifest"
                    ):
                        raise ValueError(
                            f"invalid read-set source: {member.source_asset_id}"
                        )
                    members.append(
                        member.model_copy(
                            update={
                                "output_ref": import_artifact(member.output_ref).ref,
                                "source_asset_id": asset_id,
                            }
                        )
                    )
                rebound = ReadSetManifest(
                    session_id=target_paths.session_id,
                    source_registry_ref=target_registry_ref,
                    members=members,
                )
                metadata["dependency_refs"] = [
                    target_registry_ref.model_dump(mode="json"),
                    *(member.output_ref.model_dump(mode="json") for member in members),
                ]
                staged.write_text(
                    rebound.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )
            else:
                try:
                    os.link(path, staged)
                except OSError as exc:
                    if exc.errno != EXDEV:
                        raise
                    shutil.copyfile(path, staged)
            artifact = ArtifactMeta.from_file(
                staged,
                stage="read_qc",
                method_name=record["method_id"],
                artifact_type=ref.artifact_type,
                metadata=metadata,
            )
            if not is_read_set and artifact.file_hash != ref.sha256:
                raise ValueError(
                    f"source QC artifact changed during import: {ref.path_ref}"
                )
            artifact.file_path = publish_file(staged, destination)
        [local_ref] = register_artifact_records(
            target_paths.run_dir, target_paths.session_id, [artifact]
        )
        published = PublishedArtifact(local_ref, destination)
        imported[ref.artifact_id] = published
        return published

    def rebind(value: Any) -> Any:
        if isinstance(value, dict):
            if "artifact_id" in value and "path_ref" in value:
                return import_artifact(
                    ArtifactRef.model_validate(value)
                ).ref.model_dump(mode="json")
            return {key: rebind(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rebind(item) for item in value]
        if isinstance(value, str):
            if value in asset_ids:
                return asset_ids[value]
            path = Path(value)
            if path.is_relative_to(source_paths.artifacts_dir) or value.startswith(
                "artifacts/"
            ):
                source_ref = view.ref_for_path(
                    path if path.is_absolute() else source_paths.run_dir / path
                )
                return str(import_artifact(source_ref).path)
        return value

    cursor = dict(read_read_qc_stage_output(source_state))
    cursor.pop("reuse", None)
    output = rebind(cursor)
    update = replace_read_qc_stage_output(
        state, {**output, "reuse": source.model_dump(mode="json")}
    )
    record_registered_artifacts(
        target_paths.run_dir,
        target_paths.session_id,
        (artifact.ref for artifact in imported.values()),
    )
    Ledger(target_paths.run_dir).record_event(
        target_paths.session_id,
        "read_qc_reused",
        {
            "source_session_id": source.session_id,
            "source_workflow_step_id": source_step,
        },
        stage="read_qc",
        workflow_step_id=target_step,
        idempotency_key=f"read-qc-reused:{target_step}",
    )
    return update


def _match_read_assets(source: TopLevelState, target: TopLevelState) -> dict[str, str]:
    read_types = set(
        LONG_READS_SPEC.asset_types
        + SHORT_READS_SPEC.asset_types
        + HIC_READS_SPEC.asset_types
    )
    before = load_asset_records(source, read_types)
    after = load_asset_records(target, read_types)

    def identity(record: AssetRecord) -> tuple[Any, ...]:
        return (
            record.path,
            record.asset_type,
            record.sample_id,
            record.platform,
            record.format,
            record.size_bytes,
            record.checksum,
            record.metadata,
        )

    if (
        not before
        or any(
            record.checksum_status != "ready" or not record.checksum
            for record in before + after
        )
        or [identity(record) for record in before]
        != [identity(record) for record in after]
    ):
        raise ValueError(
            "source QC and new session have different registered read inputs"
        )
    return {old.asset_id: new.asset_id for old, new in zip(before, after, strict=True)}
