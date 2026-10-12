"""fastp paired-read QC method lifecycle."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Literal, cast

from app.control.workflow_plan import current_workflow_step_id
from app.execution.attempt_owners import MethodExecution
from app.execution.env_dispatch import resolve_method_env
from app.execution.evidence import ExecutionEvidence, frozen_attempt_artifact_type
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_outcomes import (
    MethodOutcome,
    OutcomePolicy,
    has_execution_result,
    method_is_selected,
)
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    READ_QC_CLEAN_HIC_READ_R1,
    READ_QC_CLEAN_HIC_READ_R2,
    READ_QC_CLEAN_SHORT_READ_R1,
    READ_QC_CLEAN_SHORT_READ_R2,
    READ_QC_FASTP_HTML,
    READ_QC_FASTP_JSON,
    ArtifactMeta,
    ArtifactRef,
)
from app.schemas.assets import ReadSetManifest, ReadSetMember
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.execution import MethodExecutionAttempt
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import absolute_data_dir, stage_work_dir
from app.stages.read_qc.constants import (
    HIC_READS_FASTP_METHOD_NAME,
    SHORT_READS_FASTP_METHOD_NAME,
)
from app.stages.read_qc.method_application.support import (
    fail_update,
    read_qc_output,
    update_read_qc_output,
)
from app.stages.read_qc.pairs import resolve_qc_pairs
from app.storage.artifacts import (
    publish_or_recover_work_artifact,
    record_registered_artifacts,
    resolve_artifact_ref,
)
from app.storage.ledger import Ledger
from app.storage.paths import DataLayoutError
from app.workflow import is_terminal_failure


def _family_config(output: dict[str, Any], family: str) -> dict[str, Any]:
    return dict(output.get(f"{family}_request") or {})


def _method_for_family(family: str) -> str:
    return (
        SHORT_READS_FASTP_METHOD_NAME
        if family == "short_reads"
        else HIC_READS_FASTP_METHOD_NAME
    )


def fastp_outcome(family: str) -> OutcomePolicy:
    method_name = _method_for_family(family)

    def outcome(state: TopLevelState) -> MethodOutcome | None:
        if not method_is_selected(state, method_name):
            return None
        output = read_qc_output(state)
        substep = (output.get("substeps") or {}).get(family) or {}
        if substep.get("status") == "executed":
            return MethodOutcome(method_name, "completed")
        if substep.get("status") == "skipped":
            return MethodOutcome(
                method_name,
                "skipped",
                reason=str(substep.get("skip_reason") or "method_not_run"),
            )
        if is_terminal_failure(output.get("status"), extra_statuses={"blocked_input"}):
            if not has_execution_result(output, method_name):
                return MethodOutcome(method_name, "not_started")
            return MethodOutcome(
                method_name,
                "failed",
                reason=str(output.get("reason") or f"{family} fastp failed"),
            )
        return None

    return outcome


def fastp_lifecycle(family: str) -> MethodExecutionLifecycle:
    method_name = _method_for_family(family)
    return MethodExecutionLifecycle(
        stage="read_qc",
        name=f"fastp {family}",
        method_name=method_name,
        frozen_status=f"{family}_fastp_frozen",
        ran_status=f"{family}_fastp_ran",
        outcome_policy=fastp_outcome(family),
    )


def freeze_fastp_attempt(
    state: TopLevelState,
    family: str,
    lifecycle: MethodExecutionLifecycle,
) -> StateUpdate:
    output = read_qc_output(state)
    if output.get("status") != f"{family}_fastp_planned":
        return {}
    try:
        pairs = resolve_qc_pairs(state, family)
    except ValueError as exc:
        return fail_update(state, f"{family} fastp input resolution failed: {exc}")
    if not pairs:
        return update_read_qc_output(
            state,
            {
                "status": f"{family}_fastp_executed",
                "substeps": {
                    **dict(output.get("substeps") or {}),
                    family: {
                        "name": family,
                        "status": "skipped",
                        "method_name": lifecycle.method_name,
                        "skip_reason": f"no_{family}_resource",
                    },
                },
            },
        )
    request = _family_config(dict(output), family)
    working_dir = stage_work_dir(state, "read_qc", family)
    working_dir.mkdir(parents=True, exist_ok=True)
    commands: list[str] = ["set -euo pipefail"]
    expected_outputs: list[Path] = []
    metadata_pairs: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs, start=1):
        r1 = Path(pair["r1"]["path"])
        r2 = Path(pair["r2"]["path"])
        output_r1 = working_dir / f"lane_{index:04d}_R1.fastq.gz"
        output_r2 = working_dir / f"lane_{index:04d}_R2.fastq.gz"
        report_json = working_dir / f"lane_{index:04d}.json"
        report_html = working_dir / f"lane_{index:04d}.html"
        args = [
            "fastp",
            "--in1",
            str(r1),
            "--in2",
            str(r2),
            "--out1",
            str(output_r1),
            "--out2",
            str(output_r2),
            "--json",
            str(report_json),
            "--html",
            str(report_html),
            "--thread",
            str(int(request.get("threads", 4))),
            "--qualified_quality_phred",
            str(int(request.get("qualified_quality_phred", 15))),
            "--unqualified_percent_limit",
            str(int(request.get("unqualified_percent_limit", 40))),
            "--n_base_limit",
            str(int(request.get("n_base_limit", 5))),
            "--length_required",
            str(int(request.get("length_required", 50))),
        ]
        if request.get("adapter_sequence"):
            args.extend(["--adapter_sequence", str(request["adapter_sequence"])])
        if request.get("adapter_sequence_r2"):
            args.extend(["--adapter_sequence_r2", str(request["adapter_sequence_r2"])])
        if family == "hic_reads" and request.get("preserve_hic_junction", True):
            args.append("--disable_adapter_trimming")
        commands.append(" ".join(shlex.quote(arg) for arg in args))
        expected_outputs.extend([output_r1, output_r2, report_json, report_html])
        metadata_pairs.append(
            {
                **pair,
                "output_r1": str(output_r1),
                "output_r2": str(output_r2),
                "report_json": str(report_json),
                "report_html": str(report_html),
            }
        )
    from app.execution.input_integrity import (
        artifact_input_dependency,
        asset_input_dependencies,
    )
    from app.schemas.execution import InputDependency

    input_dependencies: list[InputDependency] = []
    for pair in pairs:
        for member in (pair["r1"], pair["r2"]):
            if member.get("artifact_ref") is not None:
                input_dependencies.append(
                    artifact_input_dependency(
                        state, member["artifact_ref"], member["path"]
                    )
                )
            else:
                input_dependencies.extend(
                    asset_input_dependencies(
                        state,
                        [member["source_asset_id"]],
                        [member["path"]],
                    )
                )
    env_spec, _packages, blocked = resolve_method_env(
        output,
        stage="read_qc",
        method_name=lifecycle.method_name,
        workflow_step_id=current_workflow_step_id(state, expected_stage="read_qc"),
        session_id=state["session_id"],
        data_dir=absolute_data_dir(state),
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_read_qc_output(state, blocked)
    script_path = working_dir / "run_fastp.sh"
    script_path.write_text("\n".join(commands) + "\n", encoding="utf-8")
    script_path.chmod(0o755)
    attempt = build_frozen_attempt(
        attempt_id=f"{state['session_id']}:read_qc:{family}:fastp",
        session_id=state["session_id"],
        stage="read_qc",
        method_name=lifecycle.method_name,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=expected_outputs,
        env=output.get("env"),
        env_spec=env_spec,
        input_dependencies=input_dependencies,
        metadata={"family": family, "pairs": cast(Any, metadata_pairs)},
    )
    return freeze_method_attempts(
        state,
        lifecycle,
        [attempt],
        stage_updates={
            ("short_read_pairs" if family == "short_reads" else "hic_read_pairs"): cast(
                Any, pairs
            )
        },
    )


def collect_fastp_artifacts(
    state: TopLevelState,
    family: str,
    lifecycle: MethodExecutionLifecycle,
) -> StateUpdate:
    output = read_qc_output(state)
    if output.get("status") != lifecycle.ran_status:
        return {}
    try:
        executions = load_method_executions(state, lifecycle)
    except (OSError, DataLayoutError):
        # A collect replay is allowed after the disposable method workspace
        # has been removed.  The normal owner loader validates the workspace
        # for execution; the QC fallback below hydrates only the already
        # indexed frozen-attempt and execution evidence.
        executions = _load_fastp_execution_from_index(state, lifecycle)
    if len(executions) != 1:
        return fail_update(
            state, f"{family} fastp requires exactly one execution attempt"
        )
    attempt = executions[0].attempt
    method_name = lifecycle.method_name
    r1_type, r2_type = (
        (READ_QC_CLEAN_SHORT_READ_R1, READ_QC_CLEAN_SHORT_READ_R2)
        if family == "short_reads"
        else (READ_QC_CLEAN_HIC_READ_R1, READ_QC_CLEAN_HIC_READ_R2)
    )
    published_members: list[
        tuple[dict[str, Any], ArtifactRef, ArtifactRef, Path, Path]
    ] = []
    report_refs: list[ArtifactRef] = []
    published_reports: list[Path] = []
    artifacts: list[ArtifactMeta] = []
    for pair in attempt.metadata["pairs"]:
        p1 = publish_or_recover_work_artifact(
            state.get("data_dir", "data"),
            attempt.session_id,
            Path(pair["output_r1"]),
            stage="read_qc",
            method_name=method_name,
            artifact_type=r1_type,
            metadata={"pair_id": pair["pair_id"], "mate": "R1"},
        )
        p2 = publish_or_recover_work_artifact(
            state.get("data_dir", "data"),
            attempt.session_id,
            Path(pair["output_r2"]),
            stage="read_qc",
            method_name=method_name,
            artifact_type=r2_type,
            metadata={"pair_id": pair["pair_id"], "mate": "R2"},
        )
        report_json = publish_or_recover_work_artifact(
            state.get("data_dir", "data"),
            attempt.session_id,
            Path(pair["report_json"]),
            stage="read_qc",
            method_name=method_name,
            artifact_type=READ_QC_FASTP_JSON,
            metadata={"pair_id": pair["pair_id"], "format": "json"},
        )
        report_html = publish_or_recover_work_artifact(
            state.get("data_dir", "data"),
            attempt.session_id,
            Path(pair["report_html"]),
            stage="read_qc",
            method_name=method_name,
            artifact_type=READ_QC_FASTP_HTML,
            metadata={"pair_id": pair["pair_id"], "format": "html"},
        )
        report_refs.append(report_json.ref)
        report_refs.append(report_html.ref)
        # The work directory is disposable after the run boundary.  Keep the
        # immutable published JSON path for collection/recovery; never derive
        # business summaries from the mutable attempt workspace.
        published_reports.append(report_json.path)
        published_members.append((pair, p1.ref, p2.ref, p1.path, p2.path))
        artifacts.extend(
            ArtifactMeta.from_file(
                path, stage="read_qc", method_name=method_name, artifact_type=kind
            )
            for path, kind in (
                (p1.path, r1_type),
                (p2.path, r2_type),
                (report_json.path, READ_QC_FASTP_JSON),
                (report_html.path, READ_QC_FASTP_HTML),
            )
        )
    source_registry_ref = ArtifactRef.model_validate(state.get("asset_registry_ref"))
    members: list[ReadSetMember] = []
    for pair, r1_ref, r2_ref, _r1_path, _r2_path in published_members:
        for mate, ref in (("R1", r1_ref), ("R2", r2_ref)):
            source = pair[mate.lower()]
            members.append(
                ReadSetMember(
                    output_ref=ref,
                    source_asset_id=source["source_asset_id"],
                    platform=source["platform"],
                    format="fastq",
                    compression="gzip",
                    transformation_method=method_name,
                    attempt_id=attempt.attempt_id,
                    manifest_entry_ordinal=pair["manifest_entry_ordinal"],
                    pair_index=pair["pair_index"],
                    mate=cast(Literal["R1", "R2"], mate),
                )
            )
    manifest = ReadSetManifest(
        session_id=attempt.session_id,
        source_registry_ref=source_registry_ref,
        members=members,
    )
    manifest_path = Path(attempt.working_dir) / f"{family}.read_set_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    published_manifest = publish_or_recover_work_artifact(
        state.get("data_dir", "data"),
        attempt.session_id,
        manifest_path,
        stage="read_qc",
        method_name=method_name,
        artifact_type=f"read_qc.cleaned_{family}.read_set_manifest",
        metadata={"member_count": len(members)},
    )
    artifacts.append(
        ArtifactMeta.from_file(
            published_manifest.path,
            stage="read_qc",
            method_name=method_name,
            artifact_type=f"read_qc.cleaned_{family}.read_set_manifest",
        )
    )
    summary = _summarize_reports(published_reports)
    Ledger(state.get("data_dir", "data")).record_attempt_succeeded(
        attempt.session_id,
        attempt,
        artifacts=[artifact.model_dump(mode="json") for artifact in artifacts],
    )
    record_registered_artifacts(
        state.get("data_dir", "data"),
        attempt.session_id,
        [artifact.model_dump(mode="json") for artifact in artifacts],
        attempt_id=attempt.attempt_id,
    )
    return update_read_qc_output(
        state,
        {
            "status": f"{family}_fastp_executed",
            f"{family}_clean_pairs": [
                {
                    # Do not checkpoint mutable work/report paths.  The
                    # clean-pair cursor is the handoff contract and carries
                    # only published artifact refs plus frozen source
                    # identity; collection may be replayed after work cleanup.
                    **{
                        key: value
                        for key, value in pair.items()
                        if key
                        not in {"output_r1", "output_r2", "report_json", "report_html"}
                    },
                    "r1": {
                        **pair["r1"],
                        "path": str(r1_path),
                        "compression": "gzip",
                        "artifact_ref": r1_ref.model_dump(mode="json"),
                    },
                    "r2": {
                        **pair["r2"],
                        "path": str(r2_path),
                        "compression": "gzip",
                        "artifact_ref": r2_ref.model_dump(mode="json"),
                    },
                }
                for pair, r1_ref, r2_ref, r1_path, r2_path in published_members
            ],
            f"{family}_report_artifact_refs": [
                ref.model_dump(mode="json") for ref in report_refs
            ],
            f"{family}_read_set_manifest_ref": published_manifest.ref.model_dump(
                mode="json"
            ),
            f"{family}_summary": summary,
            "substeps": {
                **dict(output.get("substeps") or {}),
                family: {
                    "name": family,
                    "status": "executed",
                    "method_name": method_name,
                    "report_artifact_refs": [
                        ref.model_dump(mode="json") for ref in report_refs
                    ],
                    "read_set_manifest_ref": published_manifest.ref.model_dump(
                        mode="json"
                    ),
                    "summary": summary,
                },
            },
        },
    )


def _summarize_reports(report_paths: list[Path]) -> dict[str, int]:
    before_reads = after_reads = before_bases = after_bases = 0
    for report_path in report_paths:
        report = _read_fastp_report(report_path)
        before = report["summary"]["before_filtering"]
        after = report["summary"]["after_filtering"]
        before_reads += before["total_reads"]
        after_reads += after["total_reads"]
        before_bases += before["total_bases"]
        after_bases += after["total_bases"]
    return {
        "lanes": len(report_paths),
        "reports": len(report_paths),
        "total_reads_before": before_reads,
        "total_reads_after": after_reads,
        "total_bases_before": before_bases,
        "total_bases_after": after_bases,
    }


def _read_fastp_report(report_path: Path) -> dict[str, dict[str, dict[str, int]]]:
    """Load the minimum fastp report contract and reject impossible counts."""

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"fastp report is not valid JSON: {report_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), dict):
        raise ValueError(f"fastp report has no summary object: {report_path}")
    summary = payload["summary"]
    result: dict[str, dict[str, dict[str, int]]] = {"summary": {}}
    for phase in ("before_filtering", "after_filtering"):
        values = summary.get(phase)
        if not isinstance(values, dict):
            raise ValueError(f"fastp report has no {phase} summary: {report_path}")
        counts: dict[str, int] = {}
        for key in ("total_reads", "total_bases"):
            value = values.get(key)
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"fastp report {phase}.{key} is not a non-negative integer: "
                    f"{report_path}"
                )
            counts[key] = value
        result["summary"][phase] = counts
    before = result["summary"]["before_filtering"]
    after = result["summary"]["after_filtering"]
    if after["total_reads"] > before["total_reads"]:
        raise ValueError(f"fastp report after reads exceed before reads: {report_path}")
    if after["total_bases"] > before["total_bases"]:
        raise ValueError(f"fastp report after bases exceed before bases: {report_path}")
    return result


def _load_fastp_execution_from_index(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
) -> list[MethodExecution]:
    """Hydrate a completed fastp execution without mutable work files."""
    output = read_qc_output(state)
    raw_attempts = output.get("execution_attempts")
    if not isinstance(raw_attempts, list) or len(raw_attempts) != 1:
        raise ValueError("fastp recovery requires exactly one execution attempt")
    cursor = MethodExecutionAttempt.model_validate(raw_attempts[0])
    if cursor.status not in {"executed", "execution_failed"}:
        raise ValueError("fastp recovery attempt is not complete")
    execution_ref = cursor.execution_ref
    if execution_ref is None:
        raise ValueError("fastp recovery attempt has no execution evidence")
    frozen_ref = cursor.frozen_attempt_ref
    if frozen_ref is None:
        raise ValueError("fastp recovery attempt has no frozen attempt evidence")
    frozen_payload = _read_indexed_json(
        state,
        frozen_ref,
        frozen_attempt_artifact_type(lifecycle.stage),
    )
    attempt = FrozenAttemptSpec.model_validate(frozen_payload.get("attempt"))
    if (
        attempt.attempt_id != cursor.attempt_id
        or attempt.method_name != lifecycle.method_name
        or attempt.stage != lifecycle.stage
        or attempt.session_id != state.get("session_id")
    ):
        raise ValueError("fastp recovery frozen attempt identity mismatch")
    evidence_ref = cursor.execution_ref
    if evidence_ref is None:
        raise ValueError("fastp recovery attempt has no execution evidence")
    evidence_payload = _read_indexed_json(
        state,
        evidence_ref,
        f"{lifecycle.stage}.execution_result",
    )
    evidence = ExecutionEvidence.model_validate(evidence_payload.get("result"))
    if evidence.attempt_id != attempt.attempt_id or not evidence.success:
        raise ValueError("fastp recovery execution evidence is not successful")
    return [
        MethodExecution(
            attempt=attempt,
            execution_ref=evidence_ref,
            evidence=evidence,
        )
    ]


def _read_indexed_json(
    state: TopLevelState,
    ref: ArtifactRef,
    expected_type: str,
) -> dict[str, Any]:
    if ref.artifact_type != expected_type:
        raise ValueError(
            f"unexpected fastp recovery artifact type: {ref.artifact_type}"
        )
    path = resolve_artifact_ref(state.get("data_dir", ""), state["session_id"], ref)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise ValueError("fastp recovery artifact is not schema v2")
    return payload
