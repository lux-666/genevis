"""Related-species evidence, statistics and notebook production."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from app.execution.database_resolution import (
    DatabaseResolutionError,
    resolve_ncbi_reference_dir,
)
from app.reference.ncbi import (
    build_related_species_evidence_pack as _build_related_species_evidence_pack,
)
from app.reference.ncbi import (
    build_related_species_statistics_pack as _build_related_species_statistics_pack,
)
from app.reference.ncbi import species_name_from_metadata
from app.schemas.artifacts import (
    ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
    ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK,
    ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
    ArtifactMeta,
    ArtifactRef,
)
from app.schemas.state import (
    StateUpdate,
    TopLevelState,
)
from app.stages.assembly_review.method_application.support import (
    write_json_artifact as _write_json_artifact,
)
from app.stages.assembly_review.notebook import (
    build_assembly_review_notebook,
    render_statistics_figures,
    write_notebook,
)
from app.stages.assembly_review.state import (
    AssemblyEvalSignalCursor,
    AssemblyReviewCursor,
    assembly_review_output,
    update_assembly_review_output,
)
from app.stages.base import (
    absolute_data_dir,
    resolve_run_ref,
    stage_artifacts_dir,
    stage_work_dir,
)
from app.storage.artifacts import (
    artifact_ref_for_path,
    publish_artifact_record,
    record_registered_artifacts,
    register_artifact_records,
)
from app.workflow import is_terminal_failure

logger = logging.getLogger(__name__)


def build_related_species_evidence_pack(state: TopLevelState) -> StateUpdate:
    """Build non-blocking local NCBI evidence for the review report and LLM."""
    ar_out = assembly_review_output(state)
    if is_terminal_failure(ar_out.get("status")):
        return {}

    species_name = _target_species_name(state)
    reference_dir = _related_species_reference_dir(ar_out, state=state)
    if reference_dir is None:
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": "frozen NCBI reference database is unavailable",
            },
        )
    evidence_pack = _build_related_species_evidence_pack(
        species_name, reference_dir=reference_dir
    )
    existing_ref = ar_out.get("related_species_evidence_pack_ref")
    if existing_ref and _json_artifact_payload_matches(existing_ref, evidence_pack):
        return {}

    evidence_ref = _write_json_artifact(
        state,
        "related_species_evidence_pack.json",
        evidence_pack,
        artifact_type=ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK,
    )
    return update_assembly_review_output(
        state,
        {
            "related_species_evidence_pack": evidence_pack,
            "related_species_evidence_pack_ref": evidence_ref,
        },
    )


def _related_species_reference_dir(
    ar_out: AssemblyReviewCursor,
    *,
    state: TopLevelState,
) -> str | None:
    """Resolve NCBI evidence exclusively from the frozen dependency contract."""
    from app.execution.dependency_snapshot import load_frozen_dependency_resolver

    try:
        resolver = load_frozen_dependency_resolver(state["data_dir"])
        return resolve_ncbi_reference_dir(
            resolver, species_name=_target_species_name(state)
        )
    except (DatabaseResolutionError, OSError, TypeError, ValueError) as exc:
        logger.error("frozen NCBI database unavailable: %s", exc)
        return None


def build_related_species_statistics_and_notebook(
    state: TopLevelState,
) -> StateUpdate:
    """Build statistics pack and comparative notebook for assembly review."""
    ar_out = assembly_review_output(state)
    if is_terminal_failure(ar_out.get("status")):
        return {}

    species_name = _target_species_name(state)
    reference_dir = _related_species_reference_dir(ar_out, state=state)
    if reference_dir is None:
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": "frozen NCBI reference database is unavailable",
            },
        )

    target_metrics = _extract_target_metrics_from_signals(ar_out)

    statistics_pack = _build_related_species_statistics_pack(
        species_name, reference_dir=reference_dir, target_metrics=target_metrics
    )

    existing_stats_ref = ar_out.get("related_species_statistics_pack_ref")
    statistics_pack_unchanged = existing_stats_ref and _json_artifact_payload_matches(
        existing_stats_ref, statistics_pack
    )

    if statistics_pack_unchanged:
        existing_notebook_ref = ar_out.get("assembly_review_comparative_notebook_ref")
        if existing_notebook_ref and Path(existing_notebook_ref).exists():
            comparative_figures = ar_out.get("comparative_figures", {})
            all_figures_exist = all(
                Path(p).exists() for p in comparative_figures.values()
            )
            if all_figures_exist:
                # Full skip: re-register artifacts for ledger consistency, then return
                assert isinstance(existing_stats_ref, str)
                data_dir = absolute_data_dir(state)
                session_id = state["session_id"]

                notebook_artifact = ArtifactMeta.from_file(
                    Path(existing_notebook_ref),
                    stage="assembly_review",
                    method_name="assembly_review.contract",
                    artifact_type=ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
                    metadata={"session_id": session_id},
                )
                register_artifact_records(data_dir, session_id, [notebook_artifact])

                figure_artifacts = [
                    ArtifactMeta.from_file(
                        Path(abs_path),
                        stage="assembly_review",
                        method_name="assembly_review.contract",
                        artifact_type=f"assembly_review.figure.{logical_name}",
                        metadata={"figure_type": logical_name},
                    )
                    for logical_name, abs_path in comparative_figures.items()
                ]
                if figure_artifacts:
                    register_artifact_records(data_dir, session_id, figure_artifacts)
                statistics_artifact_ref = artifact_ref_for_path(
                    data_dir,
                    session_id,
                    existing_stats_ref,
                    expected_type=ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
                )
                record_registered_artifacts(
                    data_dir,
                    session_id,
                    [statistics_artifact_ref, notebook_artifact, *figure_artifacts],
                )

                return {}

    if statistics_pack_unchanged:
        statistics_ref = existing_stats_ref
    else:
        statistics_ref = _write_json_artifact(
            state,
            "related_species_statistics_pack.json",
            statistics_pack,
            artifact_type=ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
        )

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    figures_dir = stage_artifacts_dir(state, "assembly_review", "figures")
    work_figures_dir = stage_work_dir(state, "assembly_review", "figures")
    figure_sources = render_statistics_figures(
        statistics_pack,
        work_figures_dir,
        target_metrics=target_metrics,
    )

    figure_paths: dict[str, str] = {}
    figure_refs: list[ArtifactRef] = []
    for logical_name, abs_path in figure_sources.items():
        source = Path(abs_path)
        artifact = ArtifactMeta.from_file(
            source,
            stage="assembly_review",
            method_name="assembly_review.contract",
            artifact_type=f"assembly_review.figure.{logical_name}",
            metadata={"figure_type": logical_name},
        )
        published = publish_artifact_record(
            data_dir,
            session_id,
            artifact,
            destination=figures_dir / source.name,
        )
        figure_paths[logical_name] = str(published.path)
        figure_refs.append(published.ref)

    out_dir = stage_artifacts_dir(state, "assembly_review")
    statistics_pack_relpath = "related_species_statistics_pack.json"
    figures_relpaths = {
        name: f"figures/{Path(path).name}" for name, path in figure_paths.items()
    }
    evidence_pack = ar_out.get("related_species_evidence_pack", {})

    notebook = build_assembly_review_notebook(
        statistics_pack=statistics_pack,
        evidence_pack=evidence_pack,
        target_metrics=target_metrics,
        session_id=session_id,
        statistics_pack_relpath=statistics_pack_relpath,
        figures_relpaths=figures_relpaths,
    )

    notebook_path = out_dir / "assembly_review_report.ipynb"
    notebook_source = stage_work_dir(
        state, "assembly_review", "assembly_review_report.ipynb"
    )
    write_notebook(notebook, notebook_source)

    notebook_artifact = ArtifactMeta.from_file(
        notebook_source,
        stage="assembly_review",
        method_name="assembly_review.contract",
        artifact_type=ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
        metadata={"session_id": session_id, "figure_count": len(figure_paths)},
    )
    published_notebook = publish_artifact_record(
        data_dir,
        session_id,
        notebook_artifact,
        destination=notebook_path,
    )
    notebook_path = published_notebook.path
    if not isinstance(statistics_ref, str):
        raise ValueError("assembly review statistics pack has no artifact path")
    statistics_artifact_ref = artifact_ref_for_path(
        data_dir,
        session_id,
        statistics_ref,
        expected_type=ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
    )
    record_registered_artifacts(
        data_dir,
        session_id,
        [statistics_artifact_ref, *figure_refs, published_notebook.ref],
    )

    return update_assembly_review_output(
        state,
        {
            "related_species_statistics_pack_ref": statistics_ref,
            "assembly_review_comparative_notebook_ref": str(notebook_path),
            "comparative_figures": figure_paths,
        },
    )


def _extract_target_metrics_from_signals(
    ar_out: AssemblyReviewCursor,
) -> dict[str, float] | None:
    """Extract target assembly metrics from available signals for percentile"""
    """computation.

    Returns None when multiple candidates exist (no single target to position).
    """
    candidates = ar_out.get("candidates", [])
    if len(candidates) != 1:
        return None

    candidate_id = candidates[0].get("candidate_id")
    if not candidate_id:
        return None

    signals = ar_out.get("signals", [])
    candidate_metrics = _candidate_metrics(candidate_id, signals)

    target_metrics: dict[str, float] = {}

    # Map QUAST metrics
    quast = candidate_metrics.get("quast", {})
    if quast.get("n50") is not None:
        target_metrics["n50_bp"] = float(quast["n50"])
    if quast.get("total_length") is not None:
        target_metrics["assembly_size_bp"] = float(quast["total_length"])
    if quast.get("gc_percent") is not None:
        target_metrics["gc_percent"] = float(quast["gc_percent"])

    # Map BUSCO metrics
    busco = candidate_metrics.get("busco", {})
    if busco.get("complete_percent") is not None:
        target_metrics["busco_complete_percent"] = float(busco["complete_percent"])
    if busco.get("duplicated_percent") is not None:
        target_metrics["busco_duplicated_percent"] = float(busco["duplicated_percent"])

    return target_metrics if target_metrics else None


def _json_artifact_payload_matches(artifact_ref: Any, payload: dict[str, Any]) -> bool:
    path = Path(str(artifact_ref))
    if not path.exists():
        return False
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return bool(existing == payload)


def _target_species_name(state: TopLevelState) -> str | None:
    """Read optional species metadata without making manifest parsing a gate."""
    manifest_ref = state.get("input_manifest_ref")
    if not manifest_ref:
        return None
    path = resolve_run_ref(state, str(manifest_ref))
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        raw = (
            json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
        )
    except (OSError, json.JSONDecodeError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    metadata = dict(raw.get("user_metadata") or {})
    return species_name_from_metadata(metadata)


def _candidate_metrics(
    candidate_id: str, signals: list[AssemblyEvalSignalCursor]
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for signal in signals:
        if signal.get("candidate_id") != candidate_id:
            continue
        if signal.get("status") != "available":
            continue
        signal_name = signal.get("signal_name")
        signal_metrics = (signal.get("metadata") or {}).get("metrics")
        if signal_name and signal_metrics:
            metrics[signal_name] = signal_metrics
    return metrics
