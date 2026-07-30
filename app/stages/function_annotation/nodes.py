"""Function annotation stage nodes for planning database-backed annotations."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from app.configuration import load_descriptor_registry
from app.execution.env_dispatch import resolve_method_env
from app.execution.input_integrity import (
    artifact_input_dependency,
    asset_input_dependencies,
    file_input_dependency,
)
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    ArtifactMeta,
    FunctionAnnotationBundle,
    FunctionAnnotationPlan,
    FunctionDatabaseProvenance,
    FunctionMethodResult,
)
from app.schemas.execution import InputDependency
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    append_route_trace,
    stage_artifacts_dir,
    stage_work_dir,
)
from app.stages.function_annotation.state import (
    FunctionAnnotationCursor,
    function_annotation_output,
    update_function_annotation_output,
)
from app.stages.resource_resolution import resolve_stage_resource
from app.stages.resource_specs import PREDICTED_PROTEINS_SPEC
from app.storage.artifacts import (
    artifact_ref_for_path,
    publish_artifact_record,
    publish_or_recover_work_artifact,
)
from app.storage.asset_registry import load_asset_records
from app.storage.ledger import Ledger
from app.workflow import next_stage_name

FUNCTION_CANDIDATE_METHODS = [
    "function_annotation.candidate.diamond",
]
FUNCTION_CONTRACT_METHOD = "function_annotation.contract"

DIAMOND_METHOD = "function_annotation.candidate.diamond"
DIAMOND_LIFECYCLE = MethodExecutionLifecycle(
    stage="function_annotation",
    name="diamond",
    method_name=DIAMOND_METHOD,
    frozen_status="diamond_frozen",
    ran_status="diamond_ran",
)

FUNCTION_EXECUTABLE_METHODS: dict[str, dict[str, str]] = {
    "function_annotation.candidate.diamond": {
        "suffix": "diamond",
        "frozen_status": "diamond_frozen",
        "ran_status": "diamond_ran",
    },
}


def build_diamond_graph() -> CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]:
    """Build the shared DIAMOND execution lifecycle."""

    return build_method_graph(
        DIAMOND_LIFECYCLE,
        freeze_diamond_attempt,
        collect_diamond_artifacts,
    )


def _function_output(state: TopLevelState) -> FunctionAnnotationCursor:
    return function_annotation_output(state)


def _update_function_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    return update_function_annotation_output(state, payload)


def _fail_update(state: TopLevelState, reason: str) -> StateUpdate:
    return _update_function_output(state, {"status": "failed", "reason": reason})


def enter_function_annotation(state: TopLevelState) -> StateUpdate:
    """Mark function annotation as running."""
    return {"supervision_state": "running_stage"}


def resolve_protein_inputs(state: TopLevelState) -> StateUpdate:
    """Resolve required predicted proteins and optional structural annotation refs."""
    if _function_output(state).get("status"):
        return {}
    proteins = resolve_stage_resource(state, resource_spec=PREDICTED_PROTEINS_SPEC)
    proteins_ref = proteins.path
    if not proteins_ref:
        return _fail_update(state, "missing proteins.fa for function_annotation")
    metadata = proteins.metadata

    return _update_function_output(
        state,
        {
            "status": "resolved",
            "proteins_fasta_ref": proteins_ref,
            "genes_gff3_ref": metadata.get("genes_gff3_ref"),
            "transcripts_fasta_ref": metadata.get("transcripts_fasta_ref"),
            "input_source": proteins.source,
        },
    )


def plan_function_methods(state: TopLevelState) -> StateUpdate:
    """Plan function annotation candidates without running tools."""
    function_out = _function_output(state)
    if function_out.get("status") != "resolved":
        return {}

    registry = load_descriptor_registry()
    missing = [
        method
        for method in FUNCTION_CANDIDATE_METHODS
        if method not in registry.methods
    ]
    if missing:
        return _fail_update(
            state, f"function_annotation descriptors missing: {missing}"
        )

    work_dir = stage_work_dir(state, "function_annotation")
    expected_outputs = {
        "gene_function_tsv": str(work_dir / "gene_function.tsv"),
        "go_annotation_table": str(work_dir / "go_annotations.tsv"),
        "kegg_annotation_table": str(work_dir / "kegg_annotations.tsv"),
        "pfam_interpro_table": str(work_dir / "pfam_interpro.tsv"),
    }
    if function_out.get("genes_gff3_ref"):
        expected_outputs["annotated_gene_models_gff3"] = str(
            work_dir / "annotated_gene_models.gff3"
        )

    return _update_function_output(
        state,
        {
            "status": "methods_planned",
            "candidate_methods": FUNCTION_CANDIDATE_METHODS,
            "method_selection_required": True,
            "expected_outputs": expected_outputs,
            "notes": [
                "execution_deferred_to_method_specific_task",
                "database_provenance_required_before_commit",
                "database_download_disabled",
            ],
        },
    )


def validate_database_provenance(state: TopLevelState) -> StateUpdate:
    """Validate database provenance and write the planning artifact."""
    function_out = _function_output(state)
    if function_out.get("status") != "methods_planned":
        return {}

    raw_provenance = function_out.get("database_provenance")
    provenance, error = _validate_provenance_records(
        raw_provenance, function_out["proteins_fasta_ref"]
    )
    if error:
        return _fail_update(state, error)
    protein_error = _validate_protein_artifact_ref(function_out["proteins_fasta_ref"])
    if protein_error:
        return _fail_update(state, protein_error)

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    plan = FunctionAnnotationPlan(
        session_id=session_id,
        status="planned",
        proteins_fasta_ref=function_out["proteins_fasta_ref"],
        genes_gff3_ref=function_out.get("genes_gff3_ref"),
        transcripts_fasta_ref=function_out.get("transcripts_fasta_ref"),
        candidate_methods=function_out.get("candidate_methods", []),
        expected_outputs=function_out.get("expected_outputs", {}),
        database_provenance=provenance,
        method_selection_required=True,
        notes=function_out.get("notes", []),
        metadata={
            "execution_enabled": False,
            "database_download_enabled": False,
            "provenance_required": True,
        },
    )
    plan_source = stage_work_dir(
        state, "function_annotation", "function_annotation_plan.json"
    )
    plan_source.parent.mkdir(parents=True, exist_ok=True)
    plan_source.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        plan_source,
        stage="function_annotation",
        method_name=FUNCTION_CONTRACT_METHOD,
        artifact_type="function_annotation.plan",
        metadata={
            "execution_enabled": False,
            "database_download_enabled": False,
            "provenance_required": True,
        },
    )
    plan_ref = publish_artifact_record(
        data_dir,
        session_id,
        artifact,
        destination=stage_artifacts_dir(
            state, "function_annotation", "function_annotation_plan.json"
        ),
    ).path
    Ledger(data_dir).record_artifact_registered(
        session_id, artifact.model_dump(mode="json")
    )
    return _update_function_output(
        state,
        {
            "status": "provenance_validated",
            "database_provenance": [
                record.model_dump(mode="json") for record in provenance
            ],
            "plan_artifact_ref": str(plan_ref),
            "artifact_refs": [str(artifact.file_path)],
        },
    )


# --- shared function method execution helpers (freeze -> run -> collect) -------


def _freeze_func_method_attempt(
    state: TopLevelState,
    *,
    method_name: str,
    cfg: dict[str, str],
    script_body: str,
    input_dependencies: list[InputDependency],
    extra_metadata: dict[str, Any] | None = None,
) -> StateUpdate:
    function_out = _function_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]

    env_spec, _packages, blocked = resolve_method_env(
        function_out,
        stage="function_annotation",
        method_name=method_name,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_function_output(state, blocked)

    working_dir = stage_work_dir(state, "function_annotation", cfg["suffix"])
    working_dir.mkdir(parents=True, exist_ok=True)

    output_tsv = working_dir / f"{cfg['suffix']}_hits.tsv"

    script_path = working_dir / f"run_{cfg['suffix']}.sh"
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:function_annotation:{cfg['suffix']}",
        session_id=session_id,
        stage="function_annotation",
        method_name=method_name,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[output_tsv],
        env=function_out.get("env"),
        env_spec=env_spec,
        input_dependencies=input_dependencies,
        metadata={
            "proteins_fasta_ref": str(function_out["proteins_fasta_ref"]),
            "output_tsv": str(output_tsv),
            **(extra_metadata or {}),
        },
    )
    return freeze_method_attempts(state, DIAMOND_LIFECYCLE, [attempt])


def _collect_func_method_artifacts(
    state: TopLevelState, *, method_name: str, cfg: dict[str, str], artifact_type: str
) -> StateUpdate:
    function_out = _function_output(state)
    executions = load_method_executions(state, DIAMOND_LIFECYCLE)
    if len(executions) != 1:
        return _fail_update(state, "DIAMOND requires exactly one execution attempt")
    attempt = executions[0].attempt
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    output_tsv = Path(str(attempt.metadata["output_tsv"]))

    published = publish_or_recover_work_artifact(
        data_dir,
        session_id,
        output_tsv,
        stage="function_annotation",
        method_name=method_name,
        artifact_type=artifact_type,
        metadata={"attempt_id": attempt.attempt_id},
    )

    Ledger(data_dir).record_attempt_succeeded(
        attempt.session_id,
        attempt,
        artifacts=[published.ref.model_dump(mode="json")],
    )

    return _update_function_output(
        state,
        {
            "status": f"{cfg['suffix']}_completed",
            f"{cfg['suffix']}_hits_ref": str(published.path),
            f"{cfg['suffix']}_hits_artifact_ref": published.ref.model_dump(mode="json"),
            "artifact_refs": [
                *function_out.get("artifact_refs", []),
                str(published.path),
            ],
        },
    )


# --- diamond method execution nodes ----------------------------------------------


def freeze_diamond_attempt(state: TopLevelState) -> StateUpdate:
    function_out = _function_output(state)
    if function_out.get("status") != "provenance_validated":
        return {}
    if function_out.get("execution_mode") != "local":
        return _update_function_output(state, {"status": "diamond_completed"})

    db_provenance = _validated_provenance_records(
        function_out.get("database_provenance")
    )
    db_path = None
    for record in db_provenance:
        if record.method_name == DIAMOND_METHOD:
            db_path = record.database_source_ref
            break
    if not db_path:
        return _update_function_output(state, {"status": "diamond_completed"})

    database_source = Path(str(db_path)).expanduser().resolve(strict=False)
    if not database_source.is_file():
        return _fail_update(
            state, f"diamond database source not found: {database_source}"
        )

    proteins_ref = function_out.get("proteins_fasta_ref")
    if not proteins_ref:
        return _fail_update(state, "diamond requires proteins_fasta_ref")

    working_dir = stage_work_dir(state, "function_annotation", "diamond")
    database_is_index = database_source.suffix.lower() == ".dmnd"
    dmnd_index = (
        database_source if database_is_index else working_dir / "reference.dmnd"
    )

    cfg = FUNCTION_EXECUTABLE_METHODS[DIAMOND_METHOD]
    output_tsv = working_dir / f"{cfg['suffix']}_hits.tsv"

    threads = function_out.get("diamond_parameters", {}).get("threads", 4)
    try:
        threads = int(threads)
    except (TypeError, ValueError):
        threads = 4

    setup_command = (
        ""
        if database_is_index
        else (
            f"diamond makedb --in {shlex.quote(str(database_source))} "
            f"-d {shlex.quote(str(dmnd_index))}\n"
        )
    )
    script = (
        "set -e\n"
        f"{setup_command}"
        f"diamond blastp --db {shlex.quote(str(dmnd_index))} "
        f"--query {shlex.quote(str(proteins_ref))}"
        f" --outfmt 6 --threads {threads} --out {shlex.quote(str(output_tsv))}\n"
        f"test -f {shlex.quote(str(output_tsv))}\n"
    )

    try:
        proteins_resolution = resolve_stage_resource(
            state,
            resource_spec=PREDICTED_PROTEINS_SPEC,
        )
        if (
            not proteins_resolution.found
            or proteins_resolution.path is None
            or Path(proteins_resolution.path).resolve(strict=False)
            != Path(str(proteins_ref)).resolve(strict=False)
        ):
            raise ValueError(
                "resolved predicted proteins no longer match proteins_fasta_ref"
            )
        if proteins_resolution.asset_records:
            proteins_dependencies = asset_input_dependencies(
                state,
                [record.asset_id for record in proteins_resolution.asset_records],
                [record.path for record in proteins_resolution.asset_records],
            )
        elif proteins_resolution.metadata.get("primary_output_ref"):
            proteins_dependencies = [
                artifact_input_dependency(
                    state,
                    proteins_resolution.metadata["primary_output_ref"],
                    proteins_ref,
                )
            ]
        elif proteins_resolution.metadata.get("source_asset_ids"):
            proteins_dependencies = asset_input_dependencies(
                state,
                [
                    str(asset_id)
                    for asset_id in proteins_resolution.metadata[
                        "source_asset_ids"
                    ]
                ],
                [proteins_ref],
            )
        else:
            raise ValueError(
                "predicted proteins resource lacks typed artifact or asset "
                "provenance"
            )

        try:
            database_artifact_ref = artifact_ref_for_path(
                Path(state.get("data_dir", "data")),
                state["session_id"],
                database_source,
            )
        except (OSError, ValueError):
            canonical_database = database_source.resolve(strict=False)
            matching_records = [
                record
                for record in load_asset_records(state)
                if Path(record.path).resolve(strict=False) == canonical_database
            ]
            if len(matching_records) > 1:
                raise ValueError(
                    "DIAMOND database source maps to multiple registered assets"
                )
            if matching_records:
                database_dependencies = asset_input_dependencies(
                    state,
                    [matching_records[0].asset_id],
                    [matching_records[0].path],
                )
            else:
                database_dependencies = [
                    file_input_dependency(database_source)
                ]
        else:
            database_dependencies = [
                artifact_input_dependency(
                    state,
                    database_artifact_ref,
                    database_source,
                )
            ]
    except (OSError, ValueError) as exc:
        return _fail_update(
            state,
            f"DIAMOND input dependency is invalid: {exc}",
        )

    return _freeze_func_method_attempt(
        state,
        method_name=DIAMOND_METHOD,
        cfg=cfg,
        script_body=script,
        input_dependencies=[
            *proteins_dependencies,
            *database_dependencies,
        ],
        extra_metadata={
            "database_source_ref": str(database_source),
            "database_index_ref": str(dmnd_index),
            "database_source_kind": "diamond_index"
            if database_is_index
            else "sequence_fasta",
            "dmnd_index": str(dmnd_index),
            "threads": threads,
        },
    )


def collect_diamond_artifacts(state: TopLevelState) -> StateUpdate:
    function_out = _function_output(state)
    if function_out.get("status") != "diamond_ran":
        return {}
    return _collect_func_method_artifacts(
        state,
        method_name=DIAMOND_METHOD,
        cfg=FUNCTION_EXECUTABLE_METHODS[DIAMOND_METHOD],
        artifact_type="function_annotation.diamond_hits",
    )


def commit_function_annotation(state: TopLevelState) -> StateUpdate:
    """Publish function annotation bundle and complete the workflow."""
    function_out = _function_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    expected_outputs = function_out.get("expected_outputs", {})
    diamond_hits_ref = function_out.get("diamond_hits_ref")
    materialized_gene_function = _existing_file_ref(
        expected_outputs.get("gene_function_tsv")
    )
    materialized_go = _existing_file_ref(expected_outputs.get("go_annotation_table"))
    materialized_kegg = _existing_file_ref(
        expected_outputs.get("kegg_annotation_table")
    )
    materialized_pfam = _existing_file_ref(expected_outputs.get("pfam_interpro_table"))
    materialized_annotated_gff = _existing_file_ref(
        expected_outputs.get("annotated_gene_models_gff3")
    )
    provenance = _validated_provenance_records(function_out.get("database_provenance"))
    method_results = _function_method_results(state, function_out, provenance)
    bundle = FunctionAnnotationBundle(
        session_id=session_id,
        status="planned",
        proteins_fasta_ref=function_out["proteins_fasta_ref"],
        genes_gff3_ref=function_out.get("genes_gff3_ref"),
        transcripts_fasta_ref=function_out.get("transcripts_fasta_ref"),
        diamond_hits_ref=diamond_hits_ref,
        gene_function_tsv_ref=materialized_gene_function,
        go_annotation_table_ref=materialized_go,
        kegg_annotation_table_ref=materialized_kegg,
        pfam_interpro_table_ref=materialized_pfam,
        annotated_gene_models_gff3_ref=materialized_annotated_gff,
        method_results=method_results,
        candidate_methods=function_out.get("candidate_methods", []),
        database_provenance=provenance,
        plan_artifact_ref=function_out.get("plan_artifact_ref"),
        notes=function_out.get("notes", []),
        metadata={
            "execution_enabled": bool(diamond_hits_ref),
            "database_download_enabled": False,
            "provenance_required": True,
        },
    )
    bundle_source = stage_work_dir(
        state, "function_annotation", "function_annotation_bundle.json"
    )
    bundle_source.parent.mkdir(parents=True, exist_ok=True)
    bundle_source.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        bundle_source,
        stage="function_annotation",
        method_name=FUNCTION_CONTRACT_METHOD,
        artifact_type="function_annotation.bundle",
        metadata={
            "execution_enabled": bool(diamond_hits_ref),
            "database_download_enabled": False,
            "provenance_required": True,
        },
    )
    bundle_ref = publish_artifact_record(
        data_dir,
        session_id,
        artifact,
        destination=stage_artifacts_dir(
            state, "function_annotation", "function_annotation_bundle.json"
        ),
    ).path
    Ledger(data_dir).record_artifact_registered(
        session_id, artifact.model_dump(mode="json")
    )
    stage_update = _update_function_output(
        state,
        {
            "status": "completed",
            "diamond_hits_ref": diamond_hits_ref,
            "gene_function_tsv_ref": materialized_gene_function,
            "go_annotation_table_ref": materialized_go,
            "kegg_annotation_table_ref": materialized_kegg,
            "pfam_interpro_table_ref": materialized_pfam,
            "annotated_gene_models_gff3_ref": materialized_annotated_gff,
            "method_results": [
                result.model_dump(mode="json") for result in method_results
            ],
            "bundle_artifact_ref": str(bundle_ref),
            "artifact_refs": [
                *function_out.get("artifact_refs", []),
                str(artifact.file_path),
            ],
        },
    )
    return {
        **stage_update,
        "current_stage": next_stage_name("function_annotation"),
        "supervision_state": "completed",
        "route_trace": append_route_trace(state, "function_annotation_stage"),
    }


def _existing_file_ref(ref: str | None) -> str | None:
    if not ref:
        return None
    path = Path(str(ref))
    return str(path) if path.is_file() else None


def _function_method_results(
    state: TopLevelState,
    function_out: FunctionAnnotationCursor,
    provenance: list[FunctionDatabaseProvenance],
) -> list[FunctionMethodResult]:
    results: list[FunctionMethodResult] = []
    diamond_hits_ref = function_out.get("diamond_hits_ref")
    if diamond_hits_ref and Path(str(diamond_hits_ref)).is_file():
        diamond_provenance = [
            record for record in provenance if record.method_name == DIAMOND_METHOD
        ]
        metadata: dict[str, Any] = {"outfmt": "6"}
        executions = load_method_executions(state, DIAMOND_LIFECYCLE)
        if len(executions) == 1:
            attempt = executions[0].attempt
            if attempt.metadata.get("dmnd_index"):
                metadata["database_index_ref"] = attempt.metadata["dmnd_index"]
            if attempt.metadata.get("database_source_ref"):
                metadata["database_source_ref"] = attempt.metadata[
                    "database_source_ref"
                ]
        results.append(
            FunctionMethodResult(
                method_name=DIAMOND_METHOD,
                status="materialized",
                result_refs={"diamond_hits": str(diamond_hits_ref)},
                database_provenance=diamond_provenance,
                metadata=metadata,
            )
        )
    return results


def fail_function_annotation(state: TopLevelState) -> StateUpdate:
    """Stop workflow on invalid function annotation inputs or provenance."""
    function_out = _function_output(state)
    return {
        "current_stage": "function_annotation",
        "supervision_state": "failed_terminal",
        "last_error": function_out.get("reason", "function_annotation failed"),
    }


def _first_value(values: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = values.get(key)
        if value:
            return str(value)
    return None


def _validate_protein_artifact_ref(proteins_ref: str) -> str | None:
    if "://" in proteins_ref:
        return (
            "proteins_fasta_ref must be a local file path for "
            f"function_annotation: {proteins_ref}"
        )
    path = Path(proteins_ref)
    if not path.exists():
        return f"protein artifact not found for function_annotation: {proteins_ref}"
    if not path.is_file():
        return f"protein artifact is not a file for function_annotation: {proteins_ref}"
    return None


def _validate_provenance_records(
    raw_provenance: object,
    proteins_ref: str,
) -> tuple[list[FunctionDatabaseProvenance], str | None]:
    if raw_provenance is None:
        return [], "missing database_provenance for function_annotation"
    if not isinstance(raw_provenance, list):
        return [], "database_provenance must be a non-empty list"
    if not raw_provenance:
        return [], "database_provenance must be a non-empty list"

    records: list[FunctionDatabaseProvenance] = []
    for index, raw_record in enumerate(raw_provenance):
        try:
            record = FunctionDatabaseProvenance.model_validate(raw_record)
        except ValidationError as exc:
            error = exc.errors()[0]
            field = ".".join(str(part) for part in error.get("loc", []))
            message = f"{field}: {error['msg']}" if field else error["msg"]
            return [], f"invalid database_provenance[{index}]: {message}"
        if record.method_name not in FUNCTION_EXECUTABLE_METHODS:
            return (
                [],
                "unsupported function_annotation provenance method: "
                f"{record.method_name}",
            )
        if record.input_proteins_ref is None:
            record = record.model_copy(update={"input_proteins_ref": proteins_ref})
        elif record.input_proteins_ref != proteins_ref:
            if not _same_local_file(
                record.input_proteins_ref,
                proteins_ref,
            ):
                return (
                    [],
                    f"database_provenance[{index}].input_proteins_ref does not "
                    "match proteins_fasta_ref",
                )
            # Registry paths are canonicalized, while a run config may retain
            # an equivalent symlink or /var -> /private/var spelling. Persist
            # the actual resolved stage input so the evidence record is exact.
            record = record.model_copy(update={"input_proteins_ref": proteins_ref})
        records.append(record)
    if not records:
        return [], "database_provenance must be a non-empty list"
    return records, None


def _same_local_file(left: str, right: str) -> bool:
    """Return whether two local path spellings identify the same file."""

    if "://" in left or "://" in right:
        return False
    left_path = Path(left).expanduser()
    right_path = Path(right).expanduser()
    try:
        return left_path.samefile(right_path)
    except OSError:
        return (
            left_path.resolve(strict=False)
            == right_path.resolve(strict=False)
            and left_path.exists()
            and right_path.exists()
        )


def _validated_provenance_records(
    raw_provenance: object,
) -> list[FunctionDatabaseProvenance]:
    """Narrow provenance after the stage's validation transition."""

    if not isinstance(raw_provenance, list):
        raise ValueError("validated function provenance must be a list")
    return [
        FunctionDatabaseProvenance.model_validate(record) for record in raw_provenance
    ]
