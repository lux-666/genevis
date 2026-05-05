"""Gene annotation stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.gene_annotation.nodes import (
    commit_gene_annotation,
    enter_gene_annotation,
    fail_gene_annotation,
    plan_evidence_merge_or_review,
    plan_gene_methods,
    resolve_gene_annotation_inputs,
    select_evidence_profile,
)
from app.stages.gene_annotation.routing import (
    route_after_evidence_profile,
    route_after_method_plan,
    route_after_resolve,
    route_after_review_plan,
)


def build_stage():
    """Build the gene annotation planning subgraph."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_gene_annotation)
    graph.add_node("resolve_gene_annotation_inputs", resolve_gene_annotation_inputs)
    graph.add_node("select_evidence_profile", select_evidence_profile)
    graph.add_node("plan_gene_methods", plan_gene_methods)
    graph.add_node("plan_evidence_merge_or_review", plan_evidence_merge_or_review)
    graph.add_node("commit", commit_gene_annotation)
    graph.add_node("fail", fail_gene_annotation)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_gene_annotation_inputs")
    graph.add_conditional_edges(
        "resolve_gene_annotation_inputs",
        route_after_resolve,
        {"profile": "select_evidence_profile", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "select_evidence_profile",
        route_after_evidence_profile,
        {"methods": "plan_gene_methods", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_gene_methods",
        route_after_method_plan,
        {"review": "plan_evidence_merge_or_review", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_evidence_merge_or_review",
        route_after_review_plan,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
