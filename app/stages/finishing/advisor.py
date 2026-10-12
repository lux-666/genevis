"""Durable source-bound finishing evidence and finite proposals."""

from __future__ import annotations

import csv
import json
import shlex
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from app.control.workflow_plan import current_workflow_step_id
from app.execution.env_dispatch import resolve_method_env
from app.execution.input_integrity import (
    artifact_input_dependency,
    asset_input_dependencies,
)
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_outcomes import MethodOutcome
from app.execution.method_runner import build_frozen_attempt
from app.llm.finishing import PARAMETERS, FinishingRecommendation, FinishingRecommender
from app.review.base import ReviewPayload
from app.review.persistence import persist_review_request
from app.schemas.artifacts import ArtifactMeta, ArtifactRef
from app.schemas.execution import InputDependency
from app.schemas.state import ActiveInterrupt, StateUpdate, TopLevelState, stage_output
from app.stages.base import stage_artifacts_dir, stage_work_dir
from app.stages.quast import validate_quast_comparison
from app.stages.read_resolution import resolve_long_read_members
from app.stages.resource_resolution import resolve_stage_resource
from app.stages.resource_specs import QUAST_REFERENCE_SPEC, assembly_resource_spec
from app.storage.artifacts import (
    publish_artifact_record,
    publish_or_recover_work_artifact,
    recover_published_artifact,
    resolve_artifact_ref,
)
from app.storage.assembly_candidates import (
    VerifiedExecutedCandidate,
    load_executed_candidate_bundle,
    publish_derived_assembly_candidate,
)
from app.storage.ledger import Ledger

GATE = "finishing_recommendation"
STAGES = ("gapcloser", "t2t", "base_polish")


def output(state: TopLevelState, stage: str) -> dict[str, Any]:
    return dict(stage_output(state, stage))


def update(state: TopLevelState, stage: str, changes: dict[str, Any]) -> StateUpdate:
    from app.stages.base_polish.state import update_base_polish_stage_output
    from app.stages.gapcloser.state import update_gapcloser_stage_output
    from app.stages.t2t.state import update_t2t_stage_output

    return {
        "gapcloser": update_gapcloser_stage_output,
        "base_polish": update_base_polish_stage_output,
        "t2t": update_t2t_stage_output,
    }[stage](state, changes)


def loop(state: TopLevelState, stage: str) -> dict[str, Any]:
    return dict(output(state, stage).get("finishing") or {})


def advisor_enabled(state: TopLevelState, stage: str) -> bool:
    from app.control.workflow_plan import current_workflow_step

    step = current_workflow_step(state)
    return bool(loop(state, stage)) or bool(
        step
        and step["allow_local_execution"]
        and output(state, stage).get("recommendation_mode") == "advisor"
    )


def work_parts(state: TopLevelState, stage: str) -> tuple[str, ...]:
    f = loop(state, stage)
    return ("attempts", f"{f['attempt']:03d}") if f else ()


def source(state: TopLevelState, stage: str) -> VerifiedExecutedCandidate:
    resource = resolve_stage_resource(state, resource_spec=assembly_resource_spec())
    candidate = load_executed_candidate_bundle(
        state["data_dir"],
        state["session_id"],
        ArtifactRef.model_validate(resource.metadata.get("candidate_bundle_ref")),
    )
    if (
        resource.paths != [str(candidate.primary_output_path)]
        or ArtifactRef.model_validate(resource.metadata.get("primary_output_ref"))
        != candidate.primary_output_ref
    ):
        raise ValueError("finishing source resource/candidate mismatch")
    recorded = loop(state, stage).get("source_ref")
    if recorded and ArtifactRef.model_validate(recorded) != candidate.ref:
        raise ValueError("finishing original source changed")
    return candidate


def load_json(state: TopLevelState, ref: Any) -> dict[str, Any]:
    path = resolve_artifact_ref(
        state["data_dir"], state["session_id"], ArtifactRef.model_validate(ref)
    )
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("finishing artifact must contain an object")
    return value


def publish_json(
    state: TopLevelState,
    stage: str,
    name: str,
    value: dict[str, Any],
    dependencies: list[Any],
) -> ArtifactRef:
    work = stage_work_dir(state, stage, "advisor", name)
    work.parent.mkdir(parents=True, exist_ok=True)
    work.write_text(json.dumps(value, sort_keys=True, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        work,
        stage=stage,
        method_name=f"{stage}.advisor",
        artifact_type=f"{stage}.advisor.{Path(name).stem}",
        metadata={"dependency_refs": dependencies},
    )
    return publish_artifact_record(
        state["data_dir"],
        state["session_id"],
        artifact,
        destination=stage_artifacts_dir(state, stage, "advisor", name),
    ).ref


def history(state: TopLevelState, stage: str) -> list[dict[str, Any]]:
    original = source(state, stage)
    records = [
        load_json(state, ref) for ref in loop(state, stage).get("history_refs", [])
    ]
    for i, record in enumerate(records):
        if (
            record["attempt"] != i
            or record["source_ref"] != original.ref.model_dump(mode="json")
            or record["stage"] != stage
        ):
            raise ValueError("finishing attempt/source history mismatch")
        PARAMETERS[stage].model_validate(record["parameters"])
        candidate = load_executed_candidate_bundle(
            state["data_dir"], state["session_id"], record["candidate_ref"]
        )
        if (
            candidate.primary_output_ref.model_dump(mode="json")
            != record["primary_ref"]
        ):
            raise ValueError("finishing candidate primary mismatch")
        for ref in record["evidence_refs"]:
            resolve_artifact_ref(
                state["data_dir"], state["session_id"], ArtifactRef.model_validate(ref)
            )
        approved = load_json(state, record["recommendation_ref"])["proposal"]
        if (
            approved["action"] != "run"
            or approved["parameters"] != record["parameters"]
        ):
            raise ValueError("finishing history differs from proposal")
        resolve_artifact_ref(
            state["data_dir"],
            state["session_id"],
            ArtifactRef.model_validate(record["decision_ref"]),
        )
    return records


def validate_proposal(
    stage: str,
    proposal: FinishingRecommendation,
    records: list[dict[str, Any]],
    maximum: int,
) -> None:
    if proposal.action == "run":
        if not isinstance(proposal.parameters, PARAMETERS[stage]):
            raise ValueError("wrong stage parameter shape")
        if len(records) >= maximum:
            raise ValueError("finishing attempt budget exhausted")
        if any(
            row["parameters"] == proposal.parameters.model_dump() for row in records
        ):
            raise ValueError("finishing proposal repeats an executed configuration")
    if proposal.action == "accept" and not any(
        row["attempt"] == proposal.selected_attempt and row["materialized"]
        for row in records
    ):
        raise ValueError(
            "finishing proposal selects an unavailable materialized candidate"
        )


def initialize(state: TopLevelState, stage: str) -> StateUpdate:
    if loop(state, stage):
        return {}
    candidate = source(state, stage)
    settings = {
        k: v
        for k, v in output(state, stage).items()
        if k not in {"status", "active_interrupt"}
    }
    config_ref = publish_json(
        state,
        stage,
        "config.json",
        {
            "stage": stage,
            "source_ref": candidate.ref.model_dump(mode="json"),
            "settings": settings,
        },
        [candidate.ref.model_dump(mode="json")],
    )
    return update(
        state,
        stage,
        {
            "finishing": {
                "phase": "evaluate",
                "source_ref": candidate.ref.model_dump(mode="json"),
                "history_refs": [],
                "attempt": 0,
                "config_ref": config_ref.model_dump(mode="json"),
            }
        },
    )


def settings(state: TopLevelState, stage: str) -> dict[str, Any]:
    f = loop(state, stage)
    record = load_json(state, f["config_ref"])
    if record["stage"] != stage or record["source_ref"] != f["source_ref"]:
        raise ValueError("finishing settings source/stage mismatch")
    return dict(record["settings"])


def maximum_attempts(state: TopLevelState, stage: str) -> int:
    return int(settings(state, stage).get("max_attempts", 3))


@lru_cache(maxsize=3)
def quast_lifecycle(stage: str) -> MethodExecutionLifecycle:
    def outcome(state: TopLevelState) -> MethodOutcome | None:
        f = loop(state, stage)
        if not f or f.get("phase") != "done":
            return None
        ref = f.get("quast_ref")
        return MethodOutcome(
            f"{stage}.eval.quast",
            "completed" if ref else "not_started",
            refs=(ArtifactRef.model_validate(ref),) if ref else (),
            reason=None if ref else "evaluation_not_started",
        )

    return MethodExecutionLifecycle(
        stage=stage,
        name=f"{stage} QUAST comparison",
        method_name=f"{stage}.eval.quast",
        frozen_status="advisor_quast_frozen" if stage != "t2t" else "alignment_frozen",
        ran_status="advisor_quast_ran" if stage != "t2t" else "alignment_ran",
        cleanup_outputs=False,
        outcome_policy=outcome,
    )


def build_quast_graph(stage: str) -> Any:
    return build_method_graph(
        quast_lifecycle(stage),
        lambda s: freeze_quast(s, stage),
        lambda s: collect_quast(s, stage),
    )


def freeze_quast(state: TopLevelState, stage: str) -> StateUpdate:
    f, cursor = loop(state, stage), output(state, stage)
    if f["phase"] != "evaluate":
        return {}
    env, _, blocked = resolve_method_env(
        cursor,
        stage=stage,
        method_name=f"{stage}.eval.quast",
        workflow_step_id=current_workflow_step_id(state, expected_stage=stage),
        session_id=state["session_id"],
        data_dir=state["data_dir"],
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked:
        return update(state, stage, {**blocked, "finishing": {**f, "phase": "failed"}})
    original, records = source(state, stage), history(state, stage)
    refs = [
        original.primary_output_ref,
        *[ArtifactRef.model_validate(row["primary_ref"]) for row in records],
    ]
    paths = [
        resolve_artifact_ref(state["data_dir"], state["session_id"], ref)
        for ref in refs
    ]
    deps: list[InputDependency] = [
        artifact_input_dependency(state, ref, path)
        for ref, path in zip(refs, paths, strict=True)
    ]
    reference = resolve_stage_resource(state, resource_spec=QUAST_REFERENCE_SPEC)
    reference_arg = ""
    if reference.found:
        if len(reference.paths) != 1 or len(reference.asset_records) != 1:
            raise ValueError("finishing QUAST requires one registered reference")
        deps.extend(
            asset_input_dependencies(
                state, [reference.asset_records[0].asset_id], reference.paths
            )
        )
        reference_arg = f"-r {shlex.quote(reference.paths[0])} "
    work = stage_work_dir(state, stage, "advisor", "evaluations", f"{len(records):03d}")
    work.mkdir(parents=True, exist_ok=True)
    report = work / "quast" / "report.tsv"
    labels = ["source", *[f"attempt_{i}" for i in range(len(records))]]
    script = work / "run_quast.sh"
    script.write_text(
        "set -euo pipefail\nquast.py "
        + " ".join(shlex.quote(str(p)) for p in paths)
        + f" {reference_arg}--labels {','.join(labels)} "
        + f"--threads {cursor.get('threads', 8)} -o {shlex.quote(str(report.parent))}\n"
        + f"test -s {shlex.quote(str(report))}\n"
    )
    attempt = build_frozen_attempt(
        attempt_id=f"{state['session_id']}:{current_workflow_step_id(state)}:quast:{len(records)}",
        session_id=state["session_id"],
        stage=stage,
        method_name=f"{stage}.eval.quast",
        script_path=script,
        working_dir=work,
        expected_outputs=[report],
        env=cursor.get("env"),
        env_spec=env,
        input_dependencies=deps,
        metadata={"labels": labels, "reference_based": reference.found},
    )
    return freeze_method_attempts(state, quast_lifecycle(stage), [attempt])


def collect_quast(state: TopLevelState, stage: str) -> StateUpdate:
    [execution] = load_method_executions(state, quast_lifecycle(stage))
    report = publish_or_recover_work_artifact(
        state["data_dir"],
        state["session_id"],
        execution.attempt.expected_outputs[0],
        stage=stage,
        method_name=f"{stage}.eval.quast",
        artifact_type=f"{stage}.quast_report",
        metadata={"dependency_refs": [execution.execution_ref.model_dump(mode="json")]},
    )
    validate_quast_comparison(report.path, execution.attempt.metadata["labels"])
    Ledger(state["data_dir"]).record_attempt_succeeded(
        state["session_id"],
        execution.attempt,
        artifacts=[report.ref.model_dump(mode="json")],
    )
    return update(
        state,
        stage,
        {
            "finishing": {
                **loop(state, stage),
                "phase": "advise",
                "quast_ref": report.ref.model_dump(mode="json"),
                "quast_execution_ref": execution.execution_ref.model_dump(mode="json"),
                "reference_based": execution.attempt.metadata["reference_based"],
            }
        },
    )


def observations(state: TopLevelState, stage: str) -> dict[str, Any]:
    """Measure one scaffold at a time; retain at most 100 diagnostic examples."""
    from app.stages.finishing.observations import measure_assembly

    return measure_assembly(
        source(state, stage).primary_output_path,
        stage=stage,
        motif=output(state, stage).get("resolved_telomere_motif"),
    )


def table_summary(path: Path) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {}
    examples: list[dict[str, Any]] = []
    numeric: dict[str, dict[str, float]] = {}
    distributions: dict[str, dict[str, int]] = {}
    n = 0
    with path.open() as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            n += 1
            if "supporting_reads" in row:
                row["supporting_read_count"] = str(
                    len(
                        {value for value in row["supporting_reads"].split(",") if value}
                    )
                )
            if len(examples) < 100:
                examples.append(row)
            for key in (
                "length",
                "mapq",
                "overlap_bp",
                "target_distance_bp",
                "extension_length",
                "left_motif_copies",
                "right_motif_copies",
                "supporting_read_count",
                "tail_motif_copies",
            ):
                if key in row and str(row[key]).isdigit():
                    val = float(row[key])
                    item = numeric.setdefault(
                        key, {"min": val, "max": val, "sum": 0.0, "count": 0.0}
                    )
                    item["min"] = min(item["min"], val)
                    item["max"] = max(item["max"], val)
                    item["sum"] += val
                    item["count"] += 1
                    integer = int(val)
                    if key == "mapq" or integer == 0:
                        bucket_label = str(integer)
                    else:
                        lower = 10 ** (len(str(integer)) - 1)
                        bucket_label = f"{lower}..{lower * 10 - 1}"
                    buckets = distributions.setdefault(key, {})
                    buckets[bucket_label] = buckets.get(bucket_label, 0) + 1
            for key in (
                "decision",
                "reason",
                "status",
                "left_has_signal",
                "right_has_signal",
            ):
                if key in row:
                    bucket = counts.setdefault(key, {})
                    bucket[row[key]] = bucket.get(row[key], 0) + 1
            for reason in row.get("rejection_reasons", "").split(";"):
                if reason:
                    bucket = counts.setdefault("rejection_reasons", {})
                    bucket[reason] = bucket.get(reason, 0) + 1
    return {
        "rows": n,
        "counts": counts,
        "numeric": {
            k: {**v, "mean": v["sum"] / v["count"]} for k, v in numeric.items()
        },
        "numeric_distributions": distributions,
        "examples": examples,
        "examples_truncated": n > len(examples),
    }


def record_attempt(state: TopLevelState, stage: str) -> StateUpdate:
    f, cursor = loop(state, stage), output(state, stage)
    records = history(state, stage)
    if len(records) != f["attempt"]:
        raise ValueError("finishing attempt history is not contiguous")
    original = source(state, stage)
    refs: list[Any] = []
    if stage == "gapcloser":
        refs = [cursor[k] for k in ("output_ref", "details_ref", "validation_ref")]
        evidence = load_json(state, cursor["validation_ref"])
        candidate = publish_derived_assembly_candidate(
            state["data_dir"],
            state["session_id"],
            workflow_step_id=current_workflow_step_id(state),
            source_candidate=original,
            primary_output_ref=ArtifactRef.model_validate(cursor["output_ref"]),
            stage=stage,
            method_name="gapcloser.tgs_gapcloser",
            action_identity=ArtifactRef.model_validate(f["decision_ref"]).artifact_id,
        )
    elif stage == "base_polish":
        refs = [
            cursor[k]
            for k in ("output_ref", "comparison_ref", "polished_candidate_ref")
        ]
        evidence = load_json(state, cursor["comparison_ref"])
        candidate = load_executed_candidate_bundle(
            state["data_dir"], state["session_id"], cursor["polished_candidate_ref"]
        )
    else:
        from app.stages.t2t.nodes import _assembly_input_dependency

        evidence = {
            "read_count": cursor.get("read_count"),
            "telomeric_read_count": cursor.get("telomeric_read_count"),
            "notes": cursor.get("notes", []),
        }
        for key in (
            "assembly_telomere_scan_ref",
            "reads_telomere_scan_ref",
            "telomere_extension_candidates_ref",
            "alignment_observations_ref",
            "patched_telomere_scan_ref",
            "telomere_status_summary_ref",
        ):
            if cursor.get(key):
                dep = _assembly_input_dependency(state, cursor[key])
                if dep.kind != "artifact":
                    raise ValueError("T2T evidence must be a persisted artifact")
                refs.append(dep.artifact_ref.model_dump(mode="json"))
                evidence[key] = table_summary(Path(cursor[key]))
        candidate = original
        if cursor.get("patched_assembly_ref"):
            dep = _assembly_input_dependency(state, cursor["patched_assembly_ref"])
            if dep.kind != "artifact":
                raise ValueError("T2T patch must be a persisted artifact")
            candidate = publish_derived_assembly_candidate(
                state["data_dir"],
                state["session_id"],
                workflow_step_id=current_workflow_step_id(state),
                source_candidate=original,
                primary_output_ref=dep.artifact_ref,
                stage=stage,
                method_name="t2t.contract",
                action_identity=ArtifactRef.model_validate(
                    f["decision_ref"]
                ).artifact_id,
            )
        refs.append(f["frozen_ref"])
    refs.append(candidate.ref.model_dump(mode="json"))
    executions = cursor.get("execution_attempts", [])
    commands = []
    for item in executions:
        frozen = load_json(state, item["frozen_attempt_ref"])
        commands.append(Path(frozen["attempt"]["script_path"]).read_text())
        for key in ("frozen_attempt_ref", "execution_ref"):
            if item.get(key):
                refs.append(item[key])
    record = {
        "stage": stage,
        "attempt": len(records),
        "source_ref": original.ref.model_dump(mode="json"),
        "candidate_ref": candidate.ref.model_dump(mode="json"),
        "primary_ref": candidate.primary_output_ref.model_dump(mode="json"),
        "materialized": candidate.ref != original.ref,
        "parameters": f["parameters"],
        "recommendation_ref": f["recommendation_ref"],
        "decision_ref": f["decision_ref"],
        "evidence": evidence,
        "evidence_refs": refs,
        "commands": commands,
        "execution_context": {
            k: f[k]
            for k in (
                "attempt",
                "parameters",
                "decision_ref",
                "recommendation_ref",
                "frozen_ref",
            )
            if k in f
        },
        "cursor": {k: v for k, v in cursor.items() if k != "finishing"},
    }
    ref = publish_json(
        state,
        stage,
        f"attempts/{len(records):03d}/assessment.json",
        record,
        [
            record["source_ref"],
            record["recommendation_ref"],
            record["decision_ref"],
            *refs,
        ],
    )
    return update(
        state,
        stage,
        {
            "finishing": {
                **f,
                "phase": "evaluate",
                "history_refs": [*f["history_refs"], ref.model_dump(mode="json")],
            },
            "execution_attempts": [],
        },
    )


def recommend(state: TopLevelState, stage: str) -> StateUpdate:
    f, cursor = loop(state, stage), output(state, stage)
    records = history(state, stage)
    original = source(state, stage)
    maximum = maximum_attempts(state, stage)
    quast = ArtifactRef.model_validate(f["quast_ref"])
    members = resolve_long_read_members(state) if stage != "t2t" else []
    evidence = {
        "stage": stage,
        "max_attempts": maximum,
        "remaining_attempts": max(0, maximum - len(records)),
        "source_method": original.bundle.method_name,
        "genome_size_prior": original.bundle.genome_size_prior,
        "source": observations(state, stage),
        "read_inputs": [m.model_dump(mode="json") for m in members]
        if members
        else cursor.get("long_read_inputs", []),
        "quast_report_tsv": resolve_artifact_ref(
            state["data_dir"], state["session_id"], quast
        ).read_text(),
        "quast_reference_based": f["reference_based"],
        "attempts": [
            {
                k: row[k]
                for k in (
                    "attempt",
                    "parameters",
                    "materialized",
                    "evidence",
                    "commands",
                )
            }
            for row in records
        ],
        "independent_quality_evidence": {
            "qv": None,
            "busco": None,
            "kmer_completeness": None,
        },
    }
    recommender = FinishingRecommender(
        stage, cast(Any, cursor.get("recommendation_output_method", "json_schema"))
    )
    deps = [
        f["config_ref"],
        original.ref.model_dump(mode="json"),
        quast.model_dump(mode="json"),
        *f["history_refs"],
    ]
    request_name = f"recommendations/{len(records):03d}/request.json"
    recovered_request = recover_published_artifact(
        state["data_dir"],
        state["session_id"],
        stage_artifacts_dir(state, stage, "advisor", request_name),
        stage=stage,
        method_name=f"{stage}.advisor",
        artifact_type=f"{stage}.advisor.request",
        metadata={"dependency_refs": deps},
    )
    if recovered_request:
        request_ref = recovered_request.ref
        request = load_json(state, request_ref)
        if (
            request.get("stage") != stage
            or request.get("workflow_step_id") != current_workflow_step_id(state)
            or request.get("dependency_refs") != deps
            or request.get("evidence") != evidence
            or request.get("output_method") != recommender.method
        ):
            raise ValueError("finishing persisted request binding mismatch")
    else:
        request = {
            "stage": stage,
            "workflow_step_id": current_workflow_step_id(state),
            "messages": recommender.messages(evidence),
            "evidence": evidence,
            "output_method": recommender.method,
            "dependency_refs": deps,
        }
        request_ref = publish_json(
            state,
            stage,
            f"recommendations/{len(records):03d}/request.json",
            request,
            deps,
        )
    name = f"recommendations/{len(records):03d}/response.json"
    recovered = recover_published_artifact(
        state["data_dir"],
        state["session_id"],
        stage_artifacts_dir(state, stage, "advisor", name),
        stage=stage,
        method_name=f"{stage}.advisor",
        artifact_type=f"{stage}.advisor.response",
        metadata={"dependency_refs": [request_ref.model_dump(mode="json")]},
    )
    if recovered:
        response_ref, response = recovered.ref, load_json(state, recovered.ref)
    else:
        result = recommender.invoke(request["messages"])
        response = {
            "request_ref": request_ref.model_dump(mode="json"),
            "proposal": result.parsed.model_dump(mode="json")
            if result.parsed
            else None,
            "error": result.error,
            "raw_response": result.raw_response,
            "raw_message": result.raw_message,
            "model_id": result.model_id,
            "model_call": result.model_call.model_dump(mode="json"),
        }
        response_ref = publish_json(
            state, stage, name, response, [request_ref.model_dump(mode="json")]
        )
    if response["request_ref"] != request_ref.model_dump(mode="json"):
        raise ValueError("finishing response request binding mismatch")
    proposal = None
    error = response.get("error")
    if response.get("proposal"):
        try:
            proposal = FinishingRecommendation.model_validate(response["proposal"])
            validate_proposal(stage, proposal, records, maximum)
        except ValueError as exc:
            proposal, error = None, str(exc)
    decisions = ["keep_prior_assembly", "stop_human_review"]
    if any(row["materialized"] for row in records):
        decisions.insert(0, "accept_candidate")
    if proposal and proposal.action != "human_review":
        decisions.insert(0, "follow_recommendation")
    context = {
        "config_ref": f["config_ref"],
        "source_ref": f["source_ref"],
        "history_refs": f["history_refs"],
        "recommendation_ref": response_ref.model_dump(mode="json"),
        "quast_ref": f["quast_ref"],
    }
    payload = ReviewPayload(
        stage=stage,
        gate_id=GATE,
        reason="finishing_recommendation_ready",
        decisions_allowed=decisions,
        auto_decidable=False,
        candidates=[
            {
                "candidate_id": f"attempt_{r['attempt']}",
                "candidate_ref": r["candidate_ref"],
            }
            for r in records
            if r["materialized"]
        ],
        candidate_comparison_context=context,
        report_refs={"quast": quast.path_ref, "recommendation": response_ref.path_ref},
        notes=[
            proposal.reason if proposal else str(error or "human judgment required")
        ],
    )
    persisted = persist_review_request(
        state,
        stage=stage,
        gate_id=GATE,
        reason=payload.reason,
        payload=payload,
        method_name=f"{stage}.advisor",
    )
    interrupt = persisted.compact_interrupt(
        stage=stage, gate_id=GATE, reason=payload.reason
    )
    Ledger(state["data_dir"]).record_review_requested(
        state["session_id"],
        stage,
        reason=payload.reason,
        review_request_artifact_ref=persisted.request_ref.path_ref,
        review_request_id=persisted.review_request_id,
        auto_decidable=False,
    )
    return {
        **update(
            state,
            stage,
            {
                "status": "blocked_human",
                "active_interrupt": interrupt,
                "finishing": {**f, **context, "phase": "blocked"},
            },
        ),
        "active_interrupt": cast(ActiveInterrupt, interrupt),
        "supervision_state": "blocked_human",
    }
