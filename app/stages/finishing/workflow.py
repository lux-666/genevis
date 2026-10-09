"""Adapt established finishing executors to a bounded evidence loop."""

from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from app.control.workflow_plan import (
    advance_workflow_step,
    assembly_delivery,
    fail_workflow_step,
)
from app.execution.lifecycle import (
    CompiledMethodGraph,
    MethodExecutionLifecycle,
    build_method_graph,
)
from app.execution.method_outcomes import MethodOutcome, settle_method_outcomes
from app.schemas.artifacts import ArtifactRef
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import append_route_trace
from app.stages.finishing.advisor import (
    build_quast_graph,
    history,
    initialize,
    load_json,
    loop,
    output,
    publish_json,
    quast_lifecycle,
    recommend,
    record_attempt,
    source,
    update,
)
from app.stages.finishing.review import approved_proposal
from app.stages.resource_resolution import update_resource_ref
from app.storage.assembly_candidates import load_executed_candidate_bundle


def enter(state: TopLevelState, stage: str) -> StateUpdate:
    f = loop(state, stage)
    if f:
        return {}
    from app.control.workflow_plan import current_workflow_step

    step = current_workflow_step(state)
    if not step or not step["allow_local_execution"]:
        return update(
            state,
            stage,
            {
                "status": "failed",
                "reason": "Finishing adviser local execution is not authorized",
            },
        )
    if stage == "gapcloser":
        from app.stages.gapcloser.nodes import enter_gapcloser

        result = enter_gapcloser(state)
    elif stage == "base_polish":
        from app.stages.base_polish.nodes import enter_base_polish

        result = enter_base_polish(state)
    else:
        from app.stages.t2t.nodes import resolve_t2t_inputs
        from app.stages.t2t.parameters.workflow import resolve_t2t_telomere_motif

        result = resolve_t2t_inputs(state)
        resolved: TopLevelState = {**state, **result}
        if output(resolved, stage).get("status") in {"failed", "failed_terminal"}:
            return result
        # The advisor needs a resolved motif even when it recommends retaining source.
        motif = resolve_t2t_telomere_motif(resolved)
        result = {**result, **motif}
    ready: TopLevelState = {**state, **result}
    if output(ready, stage).get("status") in {"failed", "failed_terminal", "no_gaps"}:
        return result
    if stage == "t2t" and output(ready, stage).get("mode") != "auto_patch":
        return update(
            ready,
            stage,
            {
                "status": "failed",
                "reason": "T2T advisor requires mode=auto_patch; use "
                "deterministic_baseline for individual manual patch selection",
            },
        )
    return {**result, **initialize(ready, stage)}


def freeze_t2t(state: TopLevelState) -> StateUpdate:
    from app.stages.base import stage_work_dir
    from app.stages.t2t.parameters.execution import (
        T2TFrozenExecutionInputs,
        load_chromosome_names_snapshot,
    )

    f, cursor = loop(state, "t2t"), output(state, "t2t")
    proposal = approved_proposal(state, "t2t")
    assert proposal.parameters is not None
    work = stage_work_dir(
        state, "t2t", "attempts", f"{f['attempt']:03d}", "t2t_finishing"
    )
    work.mkdir(parents=True, exist_ok=True)
    inputs = T2TFrozenExecutionInputs.model_validate(
        {
            "thresholds": proposal.parameters.model_dump(mode="json"),
            "resolved_telomere_motif": cursor["resolved_telomere_motif"],
            "motif_source": cursor["resolved_telomere_motif_source"],
            "mode": "auto_patch",
            "input_assembly_ref": str(source(state, "t2t").primary_output_path),
            "long_read_inputs": cursor.get("long_read_inputs", []),
            "chromosome_list_ref": cursor.get("chromosome_list_ref"),
            "chromosome_names": load_chromosome_names_snapshot(
                cursor.get("chromosome_list_ref")
            ),
            "work_dir": str(work),
            "execution_enabled": True,
        }
    )
    ref = publish_json(
        state,
        "t2t",
        f"attempts/{f['attempt']:03d}/frozen.json",
        {
            "execution_inputs": inputs.model_dump(mode="json"),
            "decision_ref": f["decision_ref"],
            "recommendation_ref": f["recommendation_ref"],
            "source_ref": f["source_ref"],
        },
        [f["decision_ref"], f["recommendation_ref"], f["source_ref"]],
    )
    return update(
        state,
        "t2t",
        {
            "status": "parameters_ready",
            "work_dir": str(work),
            "thresholds": proposal.parameters.model_dump(mode="json"),
            "finishing": {**f, "frozen_ref": ref.model_dump(mode="json")},
        },
    )


def t2t_lifecycle() -> MethodExecutionLifecycle:
    from app.stages.t2t.nodes import t2t_alignment_method_outcome

    return MethodExecutionLifecycle(
        stage="t2t",
        name="T2T adviser minimap2",
        method_name="t2t.alignment.minimap2",
        frozen_status="alignment_frozen",
        ran_status="alignment_ran",
        outcome_policy=t2t_alignment_method_outcome,
    )


def build_t2t_attempt_graph() -> CompiledMethodGraph:
    from app.stages.t2t.nodes import (
        apply_t2t_patches,
        build_t2t_candidates,
        freeze_t2t_alignment,
        prepare_t2t_reads,
        scan_assembly_telomeres,
        scan_read_telomeres,
        validate_patched_assembly,
        write_t2t_report,
    )

    graph = StateGraph(TopLevelState)
    graph.add_node("freeze_parameters", freeze_t2t)
    graph.add_node("scan", scan_assembly_telomeres)
    graph.add_node("reads", prepare_t2t_reads)
    graph.add_node("scan_reads", scan_read_telomeres)
    graph.add_node(
        "alignment",
        build_method_graph(t2t_lifecycle(), freeze_t2t_alignment, build_t2t_candidates),
    )
    graph.add_node("patch", apply_t2t_patches)
    graph.add_node("validate", validate_patched_assembly)
    graph.add_node("report", write_t2t_report)

    def resume_attempt(s: TopLevelState) -> str:
        status = str(output(s, "t2t").get("status") or "")
        return {
            "parameters_ready": "scan",
            "assembly_scanned": "reads",
            "reads_prepared": "scan_reads",
            "reads_scanned": "alignment",
            "alignment_frozen": "alignment",
            "alignment_ran": "alignment",
            "alignment_skipped": "report",
            "candidates_built": "patch",
            "patched": "validate",
            "patched_validated": "report",
            "patch_not_applied": "report",
        }.get(status, "freeze_parameters")

    graph.add_conditional_edges(
        START,
        resume_attempt,
        [
            "freeze_parameters",
            "scan",
            "reads",
            "scan_reads",
            "alignment",
            "patch",
            "validate",
            "report",
        ],
    )
    names = [
        "freeze_parameters",
        "scan",
        "reads",
        "scan_reads",
        "alignment",
        "patch",
        "validate",
        "report",
    ]
    for i, name in enumerate(names[:-1]):
        next_name = names[i + 1]
        graph.add_conditional_edges(
            name,
            lambda s, target=next_name: (
                END
                if output(s, "t2t").get("status")
                in {"failed", "failed_terminal", "blocked_env", "missing_tool"}
                else target
            ),
            [next_name, END],
        )
    graph.add_edge("report", END)
    return graph.compile()


def finish(state: TopLevelState, stage: str) -> StateUpdate:
    f = loop(state, stage)
    records = history(state, stage)
    if not f or f.get("phase") not in {"done", "failed"}:
        raise ValueError("finishing result is not approved")
    # One disposition per method per occurrence: aggregate all attempts at the end.
    refs = tuple(ArtifactRef.model_validate(r["primary_ref"]) for r in records)
    methods = {
        "gapcloser": "gapcloser.tgs_gapcloser",
        "base_polish": "base_polish.racon",
        "t2t": "t2t.alignment.minimap2",
    }
    executed = any(
        attempt.get("method_id") == methods[stage]
        and attempt.get("status") == "executed"
        for record in records
        for attempt in record["cursor"].get("execution_attempts", [])
    )
    disposition = "completed" if executed else "skipped"
    if f["phase"] == "failed" and not records:
        disposition = "not_started"
    policy = quast_lifecycle(stage).outcome_policy
    assert policy is not None
    evaluation_outcome = policy(state)
    if evaluation_outcome is None:
        evaluation_outcome = MethodOutcome(
            f"{stage}.eval.quast",
            "completed" if f.get("quast_ref") else "not_started",
            refs=(ArtifactRef.model_validate(f["quast_ref"]),)
            if f.get("quast_ref")
            else (),
            reason=None if f.get("quast_ref") else "evaluation_not_started",
        )
    assert isinstance(evaluation_outcome, MethodOutcome)
    settle_method_outcomes(
        state,
        stage=stage,
        outcomes=[
            MethodOutcome(
                methods[stage],
                cast(Any, disposition),
                refs=refs,
                reason=None
                if disposition == "completed"
                else "no biological tool execution in this occurrence"
                if disposition == "skipped"
                else "stopped by finishing review",
            ),
            evaluation_outcome,
        ],
    )
    if f["phase"] == "failed":
        return {
            **fail_workflow_step(state, stage, "stopped by finishing review"),
            **update(
                state,
                stage,
                {"status": "failed", "reason": "stopped by finishing review"},
            ),
            "active_interrupt": None,
        }
    candidate = load_executed_candidate_bundle(
        state["data_dir"], state["session_id"], f["candidate_ref"]
    )
    original = source(state, stage)
    metadata = {
        "candidate_bundle_ref": candidate.ref.model_dump(mode="json"),
        "primary_output_ref": candidate.primary_output_ref.model_dump(mode="json"),
        "finishing_history_refs": f["history_refs"],
        "finishing_decision_ref": f["decision_ref"],
    }
    if stage == "base_polish" and records:
        selected = next(
            (r for r in records if r["attempt"] == f.get("selected_attempt")),
            records[-1],
        )
        alternative = (
            original
            if candidate.ref != original.ref
            else load_executed_candidate_bundle(
                state["data_dir"], state["session_id"], selected["candidate_ref"]
            )
        )
        metadata.update(
            comparison_candidate_bundle_ref=alternative.ref.model_dump(mode="json"),
            comparison_primary_output_ref=alternative.primary_output_ref.model_dump(
                mode="json"
            ),
            polish_comparison_ref=selected["cursor"]["comparison_ref"],
        )
    if stage == "t2t":
        from app.stages.t2t.nodes import _finalize_t2t_evidence

        cursor = output(state, stage)
        if records:
            selected = next(
                (r for r in records if r["attempt"] == f.get("selected_attempt")),
                records[-1],
            )
            evidence_state: TopLevelState = {
                **state,
                **update(
                    state,
                    stage,
                    {
                        **selected["cursor"],
                        "output_assembly_ref": str(candidate.primary_output_path),
                        "decision": "accept_selected_candidates"
                        if candidate.ref != original.ref
                        else "continue_without_patch",
                        "finishing": {
                            **f,
                            "execution_context": selected["execution_context"],
                        },
                    },
                ),
            }
            finalized = _finalize_t2t_evidence(
                evidence_state, cast(Any, output(evidence_state, stage))
            )
            state = {**evidence_state, **finalized}
        else:
            # No thresholds were executed: do not manufacture a terminal scan verdict.
            state = {
                **state,
                **update(
                    state,
                    stage,
                    {
                        "status": "completed_with_warnings",
                        "notes": [
                            *cursor.get("notes", []),
                            "source_retained_without_T2T_execution",
                        ],
                        "output_assembly_ref": str(candidate.primary_output_path),
                    },
                ),
            }
    return {
        **advance_workflow_step(
            state,
            stage,
            outcome="completed",
            deliveries=(
                assembly_delivery(
                    metadata["candidate_bundle_ref"],
                    metadata["primary_output_ref"],
                    delivery_mode="materialized"
                    if candidate.ref != original.ref
                    else "reused",
                ),
            ),
        ),
        **update(state, stage, {"status": "completed", "active_interrupt": None}),
        "resource_refs": update_resource_ref(
            state, "assembly", [str(candidate.primary_output_path)], stage, metadata
        ),
        "active_interrupt": None,
        "route_trace": append_route_trace(state, f"{stage}_stage"),
    }


def build_stage(stage: str) -> CompiledMethodGraph:
    if stage == "gapcloser":
        from app.stages.gapcloser.nodes import (
            build_gapcloser_graph,
            finish_no_gaps,
        )

        executor = build_gapcloser_graph()
    elif stage == "base_polish":
        from app.stages.base_polish.nodes import build_base_polish_graph

        executor = build_base_polish_graph()
    else:
        executor = build_t2t_attempt_graph()
    graph = StateGraph(TopLevelState)
    graph.add_node("enter", lambda s: enter(s, stage))
    graph.add_node("evaluate", build_quast_graph(stage))
    graph.add_node("advise", lambda s: recommend(s, stage))
    graph.add_node("run", executor)
    graph.add_node("record", lambda s: record_attempt(s, stage))
    graph.add_node("finish", lambda s: finish(s, stage))
    graph.add_node("fail", lambda s: fail_advisor(s, stage))
    if stage == "gapcloser":
        graph.add_node("no_gaps", finish_no_gaps)
        graph.add_edge("no_gaps", END)

    def route(s: TopLevelState) -> str:
        status = output(s, stage).get("status")
        f = loop(s, stage)
        if status in {"failed", "failed_terminal", "blocked_env", "missing_tool"}:
            return "fail"
        if status == "no_gaps":
            return "no_gaps"
        phase = f.get("phase")
        if phase in {"done", "failed"}:
            return "finish"
        if phase == "blocked":
            return END
        if phase == "evaluate":
            return "evaluate"
        if phase == "advise":
            return "advise"
        if phase == "run":
            return (
                "record"
                if status
                in {
                    "review_ready",
                    "blocked_human",
                    "completed",
                    "completed_with_warnings",
                }
                else "run"
            )
        return "fail"

    graph.add_edge(START, "enter")
    destinations = ["evaluate", "advise", "run", "record", "finish", "fail", END]
    if stage == "gapcloser":
        destinations.append("no_gaps")
    for node in ("enter", "evaluate", "advise", "run", "record"):
        graph.add_conditional_edges(node, route, destinations)
    graph.add_edge("finish", END)
    graph.add_edge("fail", END)
    return graph.compile()


def fail_advisor(state: TopLevelState, stage: str) -> StateUpdate:
    from app.control.workflow_plan import current_workflow_step

    cursor = output(state, stage)
    f = loop(state, stage)
    step: dict[str, Any] = dict(current_workflow_step(state) or {})
    outcomes = []
    for method in step.get("selected_method_names", []):
        is_evaluation = method == f"{stage}.eval.quast"
        executed = [
            r
            for r in cursor.get("execution_attempts", [])
            if r.get("method_id") == method
        ]
        refs = [f["quast_ref"]] if is_evaluation and f.get("quast_ref") else []
        prior_execution = False
        if not is_evaluation:
            for raw in f.get("history_refs", []):
                record = load_json(state, raw)
                refs.append(record["primary_ref"])
                prior_execution = prior_execution or any(
                    r.get("method_id") == method and r.get("status") == "executed"
                    for r in record["cursor"].get("execution_attempts", [])
                )
        disposition = (
            "failed"
            if any(
                r.get("status") in {"executed", "execution_failed"} for r in executed
            )
            else "completed"
            if (is_evaluation and refs) or prior_execution
            else "skipped"
            if refs
            else "not_started"
        )
        outcomes.append(
            MethodOutcome(
                method,
                cast(Any, disposition),
                refs=tuple(ArtifactRef.model_validate(r) for r in refs),
                reason=None
                if disposition == "completed"
                else str(cursor.get("reason") or "finishing did not complete"),
            )
        )
    settle_method_outcomes(state, stage=stage, outcomes=outcomes)
    return {
        **fail_workflow_step(
            state, stage, str(cursor.get("reason") or "finishing failed")
        ),
        "active_interrupt": None,
    }
