"""AutoHiC-only Hi-C scaffolding stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.hic_scaffolding.method_application import build_autohic_graph
from app.stages.hic_scaffolding.nodes import (
    block_hic_requirements,
    block_hic_scaffolding,
    enter_hic_scaffolding,
    fail_hic_scaffolding,
    prepare_autohic_adoption_review,
    resolve_hic_inputs,
)
from app.stages.hic_scaffolding.parameters.workflow import (
    autohic_execution_retry_gate,
    recommend_autohic_parameters,
)
from app.stages.hic_scaffolding.routing import (
    route_after_autohic,
    route_after_enter,
    route_after_recommendation,
    route_after_resolve,
    route_after_retry_gate,
    route_after_review,
)


def build_stage():
    """Build direct AutoHiC execution with parameter and adoption gates."""

    graph = StateGraph(TopLevelState)
    graph.add_node("enter", enter_hic_scaffolding)
    graph.add_node("resolve_hic_inputs", resolve_hic_inputs)
    graph.add_node("recommend_autohic_parameters", recommend_autohic_parameters)
    graph.add_node("autohic", build_autohic_graph())
    graph.add_node("execution_retry_gate", autohic_execution_retry_gate)
    graph.add_node("chromosome_assembly_review", prepare_autohic_adoption_review)
    graph.add_node("block", block_hic_scaffolding)
    graph.add_node("block_requirements", block_hic_requirements)
    graph.add_node("fail", fail_hic_scaffolding)

    graph.add_edge(START, "enter")
    graph.add_conditional_edges(
        "enter",
        route_after_enter,
        {
            "resolve": "resolve_hic_inputs",
            "recommend": "recommend_autohic_parameters",
            "autohic": "autohic",
        },
    )
    graph.add_conditional_edges(
        "resolve_hic_inputs",
        route_after_resolve,
        {
            "recommend": "recommend_autohic_parameters",
            "requirements": "block_requirements",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "recommend_autohic_parameters",
        route_after_recommendation,
        {"block": "block", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "autohic",
        route_after_autohic,
        {
            "review": "chromosome_assembly_review",
            "retry_gate": "execution_retry_gate",
            "requirements": "block_requirements",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "execution_retry_gate",
        route_after_retry_gate,
        {"recommend": "recommend_autohic_parameters", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "chromosome_assembly_review",
        route_after_review,
        {"block": "block", "fail": "fail"},
    )
    graph.add_edge("block", END)
    graph.add_edge("block_requirements", END)
    graph.add_edge("fail", END)
    return graph.compile()
