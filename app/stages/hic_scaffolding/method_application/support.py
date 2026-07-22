"""Hi-C-specific collection helpers for the shared execution lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.execution.evidence import ExecutionEvidence
from app.execution.lifecycle import MethodExecutionLifecycle, load_method_executions
from app.schemas.artifacts import (
    HIC_QC_REPORT,
    HIC_REVIEW_BUNDLE,
    HIC_SCAFFOLDED_ASSEMBLY,
    ArtifactMeta,
)
from app.schemas.state import TopLevelState
from app.stages.base import (
    absolute_data_dir,
    ordered_unique,
    stage_output,
    update_stage_output,
)
from app.storage.artifacts import (
    publish_or_recover_work_artifact,
    register_artifact_records,
)
from app.storage.ledger import Ledger


def hic_output(state: TopLevelState) -> dict[str, Any]:
    return stage_output(state, "hic_scaffolding")


def update_hic_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    return update_stage_output(state, "hic_scaffolding", payload)


def collect_method_artifacts(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
    *,
    extra_artifacts: list[tuple[str, str]] = (),
    execution_metadata: dict[str, Any] | None = None,
) -> dict:
    """Register a Hi-C candidate, review bundle, and fallback state."""
    output = hic_output(state)
    if output.get("status") != lifecycle.ran_status:
        return {}

    [execution] = load_method_executions(state, lifecycle)
    attempt = execution.attempt
    execution_ref = execution.execution_ref
    result = execution.evidence
    data_dir = absolute_data_dir(state)
    work_output_dir = Path(str(attempt.metadata["work_output_dir"]))
    work_output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[ArtifactMeta] = []
    published_paths: dict[str, Path] = {}
    notes = list(output.get("notes") or [])

    execution_succeeded = result.success
    execution_error = result.error
    if execution_succeeded:
        scaffold_fasta = Path(str(attempt.metadata["scaffold_fasta"]))
        published = publish_or_recover_work_artifact(
            data_dir,
            attempt.session_id,
            scaffold_fasta,
            stage="hic_scaffolding",
            method_name=lifecycle.method_name,
            artifact_type=HIC_SCAFFOLDED_ASSEMBLY,
            metadata={
                "attempt_id": attempt.attempt_id,
                "review_required": True,
            },
        )
        published_paths["scaffold_fasta"] = published.path
        artifacts.append(
            ArtifactMeta.from_file(
                published.path,
                stage="hic_scaffolding",
                method_name=lifecycle.method_name,
                artifact_type=HIC_SCAFFOLDED_ASSEMBLY,
                metadata={
                    "attempt_id": attempt.attempt_id,
                    "review_required": True,
                },
            )
        )

    if execution_succeeded:
        for key, artifact_type in extra_artifacts:
            path_value = attempt.metadata.get(key)
            if path_value:
                try:
                    published = publish_or_recover_work_artifact(
                        data_dir,
                        attempt.session_id,
                        Path(str(path_value)),
                        stage="hic_scaffolding",
                        method_name=lifecycle.method_name,
                        artifact_type=artifact_type,
                        metadata={
                            "attempt_id": attempt.attempt_id,
                            "review_required": True,
                        },
                    )
                except FileNotFoundError:
                    continue
                published_paths[key] = published.path
                artifacts.append(
                    ArtifactMeta.from_file(
                        published.path,
                        stage="hic_scaffolding",
                        method_name=lifecycle.method_name,
                        artifact_type=artifact_type,
                        metadata={
                            "attempt_id": attempt.attempt_id,
                            "review_required": True,
                        },
                    )
                )

        review_bundle_source = work_output_dir / "hic_review_bundle.json"
        review_bundle_source.write_text(
            json.dumps(
                {
                    "session_id": attempt.session_id,
                    "method_name": lifecycle.method_name,
                    "status": f"{lifecycle.name}_executed_review_required",
                    "pre_hic_assembly_ref": output.get("pre_hic_assembly_ref"),
                    "output_assembly_ref": output.get(
                        "output_assembly_ref"
                    ),
                    "chromosome_scaffolded_assembly_candidate_ref": str(
                        published_paths["scaffold_fasta"]
                    ),
                    "contact_map_ref": (
                        str(published_paths["contact_map"])
                        if "contact_map" in published_paths
                        else None
                    ),
                    "assembly_file_ref": (
                        str(published_paths["assembly_file"])
                        if "assembly_file" in published_paths
                        else None
                    ),
                    "html_report_ref": (
                        str(published_paths["html_report"])
                        if "html_report" in published_paths
                        else None
                    ),
                    "review_required": True,
                    "execution_ref": execution_ref.model_dump(mode="json"),
                    "execution_summary": _execution_summary(result),
                    "execution_metadata": execution_metadata or {},
                    "notes": [
                        "geneagent_review_required_before_promotion",
                        "pre_hic_assembly_preserved_as_annotation_fallback",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        qc_report_source = work_output_dir / "hic_qc_report.json"
        qc_report_source.write_text(
            json.dumps(
                {
                    "session_id": attempt.session_id,
                    "method_name": lifecycle.method_name,
                    "status": "available",
                    "pre_hic_assembly_ref": output.get("pre_hic_assembly_ref"),
                    "chromosome_scaffolded_assembly_candidate_ref": str(
                        published_paths["scaffold_fasta"]
                    ),
                    "execution_ref": execution_ref.model_dump(mode="json"),
                    "execution_summary": _execution_summary(result),
                    "execution_metadata": execution_metadata or {},
                    "notes": [
                        "qc_thresholds_not_applied",
                        "geneagent_review_required_before_promotion",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        review_bundle_ref = publish_or_recover_work_artifact(
            data_dir,
            attempt.session_id,
            review_bundle_source,
            stage="hic_scaffolding",
            method_name=lifecycle.method_name,
            artifact_type=HIC_REVIEW_BUNDLE,
            metadata={
                "attempt_id": attempt.attempt_id,
                "review_required": True,
            },
        ).path
        qc_report_ref = publish_or_recover_work_artifact(
            data_dir,
            attempt.session_id,
            qc_report_source,
            stage="hic_scaffolding",
            method_name=lifecycle.method_name,
            artifact_type=HIC_QC_REPORT,
            metadata={
                "attempt_id": attempt.attempt_id,
                "review_required": True,
            },
        ).path
        artifacts.extend(
            [
                ArtifactMeta.from_file(
                    review_bundle_ref,
                    stage="hic_scaffolding",
                    method_name=lifecycle.method_name,
                    artifact_type=HIC_REVIEW_BUNDLE,
                    metadata={
                        "attempt_id": attempt.attempt_id,
                        "review_required": True,
                    },
                ),
                ArtifactMeta.from_file(
                    qc_report_ref,
                    stage="hic_scaffolding",
                    method_name=lifecycle.method_name,
                    artifact_type=HIC_QC_REPORT,
                    metadata={
                        "attempt_id": attempt.attempt_id,
                        "review_required": True,
                    },
                ),
            ]
        )
        notes = ordered_unique(
            [
                *notes,
                f"{lifecycle.name}_executed",
                "geneagent_review_required_before_promotion",
            ]
        )
        candidates = dict(output.get("method_candidates") or {})
        candidates[lifecycle.method_name] = {
            "status": "executed",
            "scaffold_fasta": str(published_paths["scaffold_fasta"]),
            "contact_map": (
                str(published_paths["contact_map"])
                if "contact_map" in published_paths
                else None
            ),
            "assembly_file": (
                str(published_paths["assembly_file"])
                if "assembly_file" in published_paths
                else None
            ),
            "html_report": (
                str(published_paths["html_report"])
                if "html_report" in published_paths
                else None
            ),
            "review_bundle_ref": str(review_bundle_ref),
            "qc_report_ref": str(qc_report_ref),
        }
        update = {
            "status": "autohic_review_ready",
            "chromosome_scaffolded_assembly_candidate_ref": str(
                published_paths["scaffold_fasta"]
            ),
            "hic_review_bundle_ref": str(review_bundle_ref),
            "hic_qc_report_ref": str(qc_report_ref),
            "selected_method_name": lifecycle.method_name,
            "notes": notes,
            "method_candidates": candidates,
        }
    else:
        notes = ordered_unique([*notes, f"{lifecycle.name}_execution_failed"])
        candidates = dict(output.get("method_candidates") or {})
        candidates[lifecycle.method_name] = {
            "status": "failed",
            "error": execution_error,
        }
        update = {
            "status": "failed",
            "failure_kind": "execution",
            "reason": execution_error or f"{lifecycle.name} execution failed",
            "selected_method_name": lifecycle.method_name,
            "notes": notes,
            f"{lifecycle.name}_error": execution_error,
            "method_candidates": candidates,
        }

    if artifacts:
        register_artifact_records(data_dir, attempt.session_id, artifacts)
        payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
        ledger = Ledger(data_dir)
        if execution_succeeded:
            ledger.record_attempt_succeeded(
                attempt.session_id, attempt, artifacts=payloads
            )
        for artifact in payloads:
            ledger.record_artifact_registered(
                attempt.session_id, artifact, attempt_id=attempt.attempt_id
            )

    return update_hic_output(
        state,
        {
            **update,
            "artifact_refs": ordered_unique(
                [
                    *list(output.get("artifact_refs") or []),
                    *[str(a.file_path) for a in artifacts],
                ]
            ),
        },
    )
def _execution_summary(result: ExecutionEvidence) -> dict[str, Any]:
    return {
        "success": result.success,
        "return_code": result.return_code,
        "error": _bounded_text(result.error),
    }


def _bounded_text(value: Any, *, limit: int = 2048) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
