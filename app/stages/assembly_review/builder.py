"""Assembly review stage builder.

Builds the assembly review subgraph with candidate resolution,
metric planning, rule evaluation, and human gate.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.execution.method_outcomes import MethodOutcome, method_outcome_settlement_node
from app.schemas.state import TopLevelState
from app.stages.assembly_review.method_application import (
    build_busco_graph,
    build_quast_graph,
)
from app.stages.assembly_review.method_application.busco import (
    BUSCO_METHOD,
    busco_method_outcome,
    busco_planner_outcome,
)
from app.stages.assembly_review.method_application.quast import (
    QUAST_METHOD,
    quast_method_outcome,
    quast_planner_outcome,
)
from app.stages.assembly_review.nodes import (
    enter_assembly_review,
    evaluate_review_rules,
    fail_assembly_review,
    generate_candidate_comparison,
    human_review_gate,
    parameter_quality_gate,
    plan_review_metrics,
    resolve_candidates,
)
from app.stages.assembly_review.routing import (
    route_after_busco,
    route_after_comparison,
    route_after_enter,
    route_after_parameter_quality_gate,
    route_after_plan_metrics,
    route_after_quast,
    route_from_evaluation,
)
from app.stages.assembly_review.statistics import (
    build_related_species_evidence_pack,
    build_related_species_statistics_and_notebook,
)


def build_stage() -> CompiledStateGraph[
    TopLevelState, None, TopLevelState, TopLevelState
]:
    """Build the assembly review subgraph."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_assembly_review)
    graph.add_node("resolve_candidates", resolve_candidates)
    graph.add_node("plan_metrics", plan_review_metrics)
    graph.add_node(
        "settle_planned_quast",
        method_outcome_settlement_node(
            stage="assembly_review",
            method_name=QUAST_METHOD,
            outcome_policy=quast_planner_outcome,
        ),
    )
    graph.add_node(
        "settle_planned_busco",
        method_outcome_settlement_node(
            stage="assembly_review",
            method_name=BUSCO_METHOD,
            outcome_policy=busco_planner_outcome,
        ),
    )
    graph.add_node(
        "settle_review_methods_before_failure",
        method_outcome_settlement_node(
            stage="assembly_review",
            outcome_policy=_terminal_review_method_outcomes,
        ),
    )
    graph.add_node("quast", build_quast_graph())
    graph.add_node("busco", build_busco_graph())
    graph.add_node("evaluate_rules", evaluate_review_rules)
    graph.add_node("parameter_quality_gate", parameter_quality_gate)
    graph.add_node("related_species_sanity_check", build_related_species_evidence_pack)
    graph.add_node(
        "related_species_statistics", build_related_species_statistics_and_notebook
    )
    graph.add_node("generate_comparison", generate_candidate_comparison)
    graph.add_node("human_gate", human_review_gate)
    graph.add_node("fail", fail_assembly_review)

    graph.add_edge(START, "enter")
    graph.add_conditional_edges(
        "enter",
        route_after_enter,
        {
            "resolve_candidates": "resolve_candidates",
            "fail": "settle_review_methods_before_failure",
        },
    )
    graph.add_edge("resolve_candidates", "plan_metrics")
    graph.add_edge("plan_metrics", "settle_planned_quast")
    graph.add_edge("settle_planned_quast", "settle_planned_busco")
    graph.add_conditional_edges(
        "settle_planned_busco",
        route_after_plan_metrics,
        {
            "quast": "quast",
            "evaluate_rules": "evaluate_rules",
            "fail": "settle_review_methods_before_failure",
        },
    )
    graph.add_conditional_edges(
        "quast",
        route_after_quast,
        {
            "busco": "busco",
            "evaluate_rules": "evaluate_rules",
            "fail": "settle_review_methods_before_failure",
        },
    )
    graph.add_conditional_edges(
        "busco",
        route_after_busco,
        {
            "evaluate_rules": "evaluate_rules",
            "fail": "settle_review_methods_before_failure",
        },
    )
    graph.add_conditional_edges(
        "evaluate_rules",
        route_after_parameter_quality_gate,
        {
            "parameter_quality_gate": "parameter_quality_gate",
            "related_species_sanity_check": "related_species_sanity_check",
            "fail": "settle_review_methods_before_failure",
        },
    )
    graph.add_conditional_edges(
        "parameter_quality_gate",
        route_after_parameter_quality_gate,
        {
            "related_species_sanity_check": "related_species_sanity_check",
            "fail": "settle_review_methods_before_failure",
        },
    )
    graph.add_edge("related_species_sanity_check", "related_species_statistics")
    graph.add_conditional_edges(
        "related_species_statistics",
        route_from_evaluation,
        {
            "generate_comparison": "generate_comparison",
            "human_gate": "human_gate",
            "fail": "settle_review_methods_before_failure",
        },
    )
    graph.add_conditional_edges(
        "generate_comparison",
        route_after_comparison,
        {"human_gate": "human_gate", "fail": "settle_review_methods_before_failure"},
    )
    graph.add_edge("human_gate", END)
    graph.add_edge("settle_review_methods_before_failure", "fail")
    graph.add_edge("fail", END)

    return graph.compile()


def _terminal_review_method_outcomes(
    state: TopLevelState,
) -> tuple[MethodOutcome, ...]:
    """Settle both frozen review evaluators before an occurrence terminal."""

    return tuple(
        outcome
        for outcome in (quast_method_outcome(state), busco_method_outcome(state))
        if outcome is not None
    )
