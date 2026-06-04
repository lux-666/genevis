"""Gene annotation stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.gene_annotation.nodes import (
    block_candidate_evidence_review,
    block_final_gene_set_review,
    block_gene_method_planning_review,
    block_merge_review,
    collect_augustus_artifacts,
    collect_evm_artifacts,
    collect_gene_busco_artifacts,
    collect_miniprot_artifacts,
    commit_gene_annotation,
    enter_gene_annotation,
    fail_gene_annotation,
    freeze_augustus_attempt,
    freeze_evm_attempt,
    freeze_gene_busco_attempt,
    freeze_miniprot_attempt,
    plan_evidence_merge_or_review,
    plan_gene_methods,
    resolve_gene_annotation_inputs,
    run_augustus_attempt,
    run_evm_attempt,
    run_gene_busco_attempt,
    run_gene_methods_planning_only,
    run_miniprot_attempt,
    select_evidence_profile,
)
from app.stages.gene_annotation.routing import (
    route_after_busco_collect,
    route_after_busco_freeze,
    route_after_busco_run,
    route_after_candidate_gate,
    route_after_enter,
    route_after_evm_collect,
    route_after_evm_freeze,
    route_after_evm_run,
    route_after_evidence_profile,
    route_after_final_gate,
    route_after_merge_gate,
    route_after_merge_prep,
    route_after_method_collect,
    route_after_method_plan,
    route_after_method_planning_gate,
    route_after_freeze_for,
    route_after_run_for,
    route_after_resolve,
    route_after_run_methods,
)


def build_stage():
    """Build the gene annotation subgraph with its review chain.

    Each gate decision, the planning-only stub, method execution nodes,
    and merge prep are individual graph nodes so LangGraph can checkpoint
    between them.  Method execution dispatch is data-driven via
    ``GENE_EXECUTABLE_METHODS`` — adding a new executable method only
    requires the three freeze / run / collect nodes plus an entry in that
    dict and the corresponding builder edge wiring.
    """
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_gene_annotation)
    graph.add_node("resolve_gene_annotation_inputs", resolve_gene_annotation_inputs)
    graph.add_node("select_evidence_profile", select_evidence_profile)
    graph.add_node("plan_gene_methods", plan_gene_methods)
    graph.add_node("block_gene_method_planning_review", block_gene_method_planning_review)
    graph.add_node("run_gene_methods_planning_only", run_gene_methods_planning_only)
    # method execution — miniprot
    graph.add_node("freeze_miniprot_attempt", freeze_miniprot_attempt)
    graph.add_node("run_miniprot_attempt", run_miniprot_attempt)
    graph.add_node("collect_miniprot_artifacts", collect_miniprot_artifacts)
    # method execution — augustus
    graph.add_node("freeze_augustus_attempt", freeze_augustus_attempt)
    graph.add_node("run_augustus_attempt", run_augustus_attempt)
    graph.add_node("collect_augustus_artifacts", collect_augustus_artifacts)
    # EVM merge + BUSCO execution
    graph.add_node("freeze_evm_attempt", freeze_evm_attempt)
    graph.add_node("run_evm_attempt", run_evm_attempt)
    graph.add_node("collect_evm_artifacts", collect_evm_artifacts)
    graph.add_node("freeze_gene_busco_attempt", freeze_gene_busco_attempt)
    graph.add_node("run_gene_busco_attempt", run_gene_busco_attempt)
    graph.add_node("collect_gene_busco_artifacts", collect_gene_busco_artifacts)
    # review chain
    graph.add_node("block_candidate_evidence_review", block_candidate_evidence_review)
    graph.add_node("plan_evidence_merge_or_review", plan_evidence_merge_or_review)
    graph.add_node("block_merge_review", block_merge_review)
    graph.add_node("block_final_gene_set_review", block_final_gene_set_review)
    graph.add_node("commit", commit_gene_annotation)
    graph.add_node("fail", fail_gene_annotation)

    graph.add_edge(START, "enter")
    graph.add_conditional_edges(
        "enter",
        route_after_enter,
        {"resolve": "resolve_gene_annotation_inputs", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "resolve_gene_annotation_inputs",
        route_after_resolve,
        {"profile": "select_evidence_profile", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "select_evidence_profile",
        route_after_evidence_profile,
        {"methods": "plan_gene_methods", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_gene_methods",
        route_after_method_plan,
        {
            "block_method": "block_gene_method_planning_review",
            "run_methods": "run_gene_methods_planning_only",
            "block_candidates": "block_candidate_evidence_review",
            "merge_prep": "plan_evidence_merge_or_review",
            "block_merge": "block_merge_review",
            "block_final": "block_final_gene_set_review",
            "evm_freeze": "freeze_evm_attempt",
            "evm_run": "run_evm_attempt",
            "evm_collect": "collect_evm_artifacts",
            "busco_freeze": "freeze_gene_busco_attempt",
            "busco_run": "run_gene_busco_attempt",
            "busco_collect": "collect_gene_busco_artifacts",
            "commit": "commit",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "block_gene_method_planning_review",
        route_after_method_planning_gate,
        {"run_methods": "run_gene_methods_planning_only", "end": END, "fail": "fail"},
    )
    graph.add_conditional_edges(
        "run_gene_methods_planning_only",
        route_after_run_methods,
        {
            "block_candidates": "block_candidate_evidence_review",
            "miniprot_freeze": "freeze_miniprot_attempt",
            "miniprot_run": "run_miniprot_attempt",
            "miniprot_collect": "collect_miniprot_artifacts",
            "augustus_freeze": "freeze_augustus_attempt",
            "augustus_run": "run_augustus_attempt",
            "augustus_collect": "collect_augustus_artifacts",
            "fail": "fail",
        },
    )
    # --- miniprot edges ---
    graph.add_conditional_edges(
        "freeze_miniprot_attempt",
        route_after_freeze_for("miniprot"),
        {"miniprot_run": "run_miniprot_attempt", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "run_miniprot_attempt",
        route_after_run_for("miniprot"),
        {"miniprot_collect": "collect_miniprot_artifacts", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "collect_miniprot_artifacts",
        route_after_method_collect,
        {"block_candidates": "block_candidate_evidence_review", "run_methods": "run_gene_methods_planning_only", "fail": "fail"},
    )
    # --- augustus edges ---
    graph.add_conditional_edges(
        "freeze_augustus_attempt",
        route_after_freeze_for("augustus"),
        {"augustus_run": "run_augustus_attempt", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "run_augustus_attempt",
        route_after_run_for("augustus"),
        {"augustus_collect": "collect_augustus_artifacts", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "collect_augustus_artifacts",
        route_after_method_collect,
        {"block_candidates": "block_candidate_evidence_review", "run_methods": "run_gene_methods_planning_only", "fail": "fail"},
    )
    # --- EVM / BUSCO edges ---
    graph.add_conditional_edges(
        "freeze_evm_attempt",
        route_after_evm_freeze,
        {"evm_run": "run_evm_attempt", "busco_freeze": "freeze_gene_busco_attempt", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "run_evm_attempt",
        route_after_evm_run,
        {"evm_collect": "collect_evm_artifacts", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "collect_evm_artifacts",
        route_after_evm_collect,
        {"busco_freeze": "freeze_gene_busco_attempt", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "freeze_gene_busco_attempt",
        route_after_busco_freeze,
        {"busco_run": "run_gene_busco_attempt", "block_final": "block_final_gene_set_review", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "run_gene_busco_attempt",
        route_after_busco_run,
        {"busco_collect": "collect_gene_busco_artifacts", "block_final": "block_final_gene_set_review", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "collect_gene_busco_artifacts",
        route_after_busco_collect,
        {"block_final": "block_final_gene_set_review"},
    )
    # --- review chain ---
    graph.add_conditional_edges(
        "block_candidate_evidence_review",
        route_after_candidate_gate,
        {"merge_prep": "plan_evidence_merge_or_review", "run_methods": "run_gene_methods_planning_only", "end": END, "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_evidence_merge_or_review",
        route_after_merge_prep,
        {"block_merge": "block_merge_review", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "block_merge_review",
        route_after_merge_gate,
        {"evm_freeze": "freeze_evm_attempt", "block_final": "block_final_gene_set_review", "end": END, "fail": "fail"},
    )
    graph.add_conditional_edges(
        "block_final_gene_set_review",
        route_after_final_gate,
        {"commit": "commit", "merge_prep": "plan_evidence_merge_or_review", "end": END, "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
