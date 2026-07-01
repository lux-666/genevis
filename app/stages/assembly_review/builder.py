"""Assembly review stage builder.

Builds the assembly review subgraph with candidate resolution,
metric planning, rule evaluation, and human gate.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.assembly_review.method_application import build_busco_graph, build_quast_graph
from app.stages.assembly_review.nodes import (
    build_related_species_evidence_pack,
    build_related_species_statistics_and_notebook,
    enter_assembly_review,
    evaluate_review_rules,
    fail_assembly_review,
    generate_candidate_comparison,
    human_review_gate,
    plan_review_metrics,
    resolve_candidates,
)
from app.stages.assembly_review.routing import (
    route_after_comparison,
    route_after_plan_metrics,
    route_after_busco,
    route_after_quast,
    route_from_evaluation,
)


def build_stage():
    """Build the assembly review subgraph."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_assembly_review)
    graph.add_node("resolve_candidates", resolve_candidates)
    graph.add_node("plan_metrics", plan_review_metrics)
    graph.add_node("quast", build_quast_graph())
    graph.add_node("busco", build_busco_graph())
    graph.add_node("evaluate_rules", evaluate_review_rules)
    graph.add_node("related_species_sanity_check", build_related_species_evidence_pack)
    graph.add_node("related_species_statistics", build_related_species_statistics_and_notebook)
    graph.add_node("generate_comparison", generate_candidate_comparison)
    graph.add_node("human_gate", human_review_gate)
    graph.add_node("fail", fail_assembly_review)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_candidates")
    graph.add_edge("resolve_candidates", "plan_metrics")
    graph.add_conditional_edges(
        "plan_metrics",
        route_after_plan_metrics,
        {
            "quast": "quast",
            "evaluate_rules": "evaluate_rules",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "quast",
        route_after_quast,
        {"busco": "busco", "evaluate_rules": "evaluate_rules", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "busco",
        route_after_busco,
        {"evaluate_rules": "evaluate_rules", "fail": "fail"},
    )
    graph.add_edge("evaluate_rules", "related_species_sanity_check")
    graph.add_edge("related_species_sanity_check", "related_species_statistics")
    graph.add_conditional_edges(
        "related_species_statistics",
        route_from_evaluation,
        {
            "generate_comparison": "generate_comparison",
            "human_gate": "human_gate",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "generate_comparison",
        route_after_comparison,
        {"human_gate": "human_gate", "fail": "fail"},
    )
    graph.add_edge("human_gate", END)
    graph.add_edge("fail", END)

    return graph.compile()
