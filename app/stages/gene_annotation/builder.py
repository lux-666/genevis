"""Gene annotation stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.schemas.state import TopLevelState
from app.stages.gene_annotation.nodes import (
    block_candidate_evidence_review,
    block_final_gene_set_review,
    block_gene_method_planning_review,
    block_merge_review,
    build_augustus_graph,
    build_evm_graph,
    build_gene_busco_graph,
    build_gffread_graph,
    build_miniprot_graph,
    commit_gene_annotation,
    enter_gene_annotation,
    fail_gene_annotation,
    plan_evidence_merge_or_review,
    plan_gene_methods,
    prepare_gene_method_execution,
    resolve_gene_annotation_inputs,
    select_evidence_profile,
)
from app.stages.gene_annotation.routing import (
    route_after_busco,
    route_after_candidate_gate,
    route_after_candidate_method,
    route_after_enter,
    route_after_evidence_profile,
    route_after_evm,
    route_after_final_gate,
    route_after_materialization,
    route_after_merge_gate,
    route_after_merge_prep,
    route_after_method_plan,
    route_after_method_planning_gate,
    route_after_resolve,
    route_after_run_methods,
)


def build_stage() -> CompiledStateGraph[
    TopLevelState, None, TopLevelState, TopLevelState
]:
    """Build the gene annotation subgraph with its review chain.

    Review decisions and merge preparation remain stage-owned. Executable
    methods enter the shared freeze -> run -> collect subgraphs.
    """
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_gene_annotation)
    graph.add_node("resolve_gene_annotation_inputs", resolve_gene_annotation_inputs)
    graph.add_node("select_evidence_profile", select_evidence_profile)
    graph.add_node("plan_gene_methods", plan_gene_methods)
    graph.add_node(
        "block_gene_method_planning_review", block_gene_method_planning_review
    )
    graph.add_node("prepare_gene_method_execution", prepare_gene_method_execution)
    graph.add_node("miniprot", build_miniprot_graph())
    graph.add_node("augustus", build_augustus_graph())
    graph.add_node("materialize", build_gffread_graph())
    graph.add_node("evm", build_evm_graph())
    graph.add_node("busco", build_gene_busco_graph())
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
            "run_methods": "prepare_gene_method_execution",
            "block_candidates": "block_candidate_evidence_review",
            "merge_prep": "plan_evidence_merge_or_review",
            "block_merge": "block_merge_review",
            "block_final": "block_final_gene_set_review",
            "materialize": "materialize",
            "evm": "evm",
            "busco": "busco",
            "commit": "commit",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "block_gene_method_planning_review",
        route_after_method_planning_gate,
        {"run_methods": "prepare_gene_method_execution", "end": END, "fail": "fail"},
    )
    graph.add_conditional_edges(
        "prepare_gene_method_execution",
        route_after_run_methods,
        {
            "block_candidates": "block_candidate_evidence_review",
            "miniprot": "miniprot",
            "augustus": "augustus",
            "materialize": "materialize",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "miniprot",
        route_after_candidate_method,
        {"materialize": "materialize", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "augustus",
        route_after_candidate_method,
        {"materialize": "materialize", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "materialize",
        route_after_materialization,
        {
            "block_candidates": "block_candidate_evidence_review",
            "run_methods": "prepare_gene_method_execution",
            "busco": "busco",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "evm",
        route_after_evm,
        {"materialize": "materialize", "busco": "busco", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "busco",
        route_after_busco,
        {"block_final": "block_final_gene_set_review", "fail": "fail"},
    )
    # --- review chain ---
    graph.add_conditional_edges(
        "block_candidate_evidence_review",
        route_after_candidate_gate,
        {
            "merge_prep": "plan_evidence_merge_or_review",
            "run_methods": "prepare_gene_method_execution",
            "end": END,
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "plan_evidence_merge_or_review",
        route_after_merge_prep,
        {"block_merge": "block_merge_review", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "block_merge_review",
        route_after_merge_gate,
        {
            "evm": "evm",
            "block_final": "block_final_gene_set_review",
            "end": END,
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "block_final_gene_set_review",
        route_after_final_gate,
        {
            "commit": "commit",
            "merge_prep": "plan_evidence_merge_or_review",
            "end": END,
            "fail": "fail",
        },
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
