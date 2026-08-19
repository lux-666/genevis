"""Assembly stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.execution.method_outcomes import method_outcome_settlement_node
from app.schemas.state import TopLevelState
from app.stages.assembly.method_application import (
    build_flye_graph,
    build_hifiasm_graph,
    build_hifiasm_ont_graph,
    build_nextdenovo_graph,
)
from app.stages.assembly.method_application.support import (
    assembly_terminal_method_outcome_policy,
)
from app.stages.assembly.nodes import (
    block_assembly_for_human,
    build_assembly_candidate_set,
    commit_assembly_plan,
    enter_assembly,
    fail_assembly,
    infer_input_profile,
    plan_candidate_attempts,
    resolve_extended_inputs,
    select_primary,
    select_route,
)
from app.stages.assembly.parameters.workflow import (
    execution_retry_gate,
    recommend_assembly_parameters,
)
from app.stages.assembly.routing import (
    route_after_candidate_set,
    route_after_commit,
    route_after_enter,
    route_after_execution_retry_gate,
    route_after_infer_profile,
    route_after_method_subgraph,
    route_after_parameter_recommendation,
    route_after_plan,
    route_after_resolve,
    route_after_select,
    route_after_select_route,
)


def build_stage() -> CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]:
    """Build the assembly candidate-planning subgraph."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_assembly)
    graph.add_node("resolve_extended_inputs", resolve_extended_inputs)
    graph.add_node("infer_input_profile", infer_input_profile)
    graph.add_node("select_route", select_route)
    graph.add_node("build_assembly_candidate_set", build_assembly_candidate_set)
    graph.add_node("select_primary", select_primary)
    graph.add_node("recommend_parameters", recommend_assembly_parameters)
    graph.add_node("plan_candidate_attempts", plan_candidate_attempts)
    graph.add_node("flye", build_flye_graph())
    graph.add_node("nextdenovo", build_nextdenovo_graph())
    graph.add_node("hifiasm", build_hifiasm_graph())
    graph.add_node("hifiasm_ont", build_hifiasm_ont_graph())
    graph.add_node("execution_retry_gate", execution_retry_gate)
    graph.add_node("commit", commit_assembly_plan)
    graph.add_node("fail", fail_assembly)
    graph.add_node(
        "settle_method_outcomes",
        method_outcome_settlement_node(
            stage="assembly",
            outcome_policy=assembly_terminal_method_outcome_policy,
        ),
    )
    graph.add_node("block", block_assembly_for_human)

    graph.add_edge(START, "enter")
    graph.add_conditional_edges(
        "enter",
        route_after_enter,
        {
            "resolve": "resolve_extended_inputs",
            "recommend": "recommend_parameters",
            "plan": "plan_candidate_attempts",
        },
    )
    graph.add_conditional_edges(
        "resolve_extended_inputs",
        route_after_resolve,
        {"infer_profile": "infer_input_profile", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "infer_input_profile",
        route_after_infer_profile,
        {"select_route": "select_route", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "select_route",
        route_after_select_route,
        {"build": "build_assembly_candidate_set", "block": "block", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "build_assembly_candidate_set",
        route_after_candidate_set,
        {"select": "select_primary", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "select_primary",
        route_after_select,
        {"recommend": "recommend_parameters", "block": "block", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "recommend_parameters",
        route_after_parameter_recommendation,
        {"plan": "plan_candidate_attempts", "block": "block", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_candidate_attempts",
        route_after_plan,
        {
            "flye": "flye",
            "nextdenovo": "nextdenovo",
            "hifiasm": "hifiasm",
            "hifiasm_ont": "hifiasm_ont",
            "commit": "commit",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "flye",
        route_after_method_subgraph,
        {"retry_gate": "execution_retry_gate", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "nextdenovo",
        route_after_method_subgraph,
        {"retry_gate": "execution_retry_gate", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "hifiasm",
        route_after_method_subgraph,
        {"retry_gate": "execution_retry_gate", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "hifiasm_ont",
        route_after_method_subgraph,
        {"retry_gate": "execution_retry_gate", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "execution_retry_gate",
        route_after_execution_retry_gate,
        {"recommend": "recommend_parameters", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "commit",
        route_after_commit,
        {"settle": "settle_method_outcomes", "done": END},
    )
    graph.add_edge("fail", "settle_method_outcomes")
    graph.add_edge("settle_method_outcomes", END)
    graph.add_edge("block", END)

    return graph.compile()
