"""Repeat annotation stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.schemas.state import TopLevelState
from app.stages.repeat_annotation.nodes import (
    block_mask_review,
    build_repeatmasker_graph,
    commit_repeat_annotation,
    enter_repeat_annotation,
    fail_repeat_annotation,
    plan_repeat_library,
    plan_repeat_masking,
    resolve_repeat_inputs,
    select_repeat_strategy,
)
from app.stages.repeat_annotation.routing import (
    route_after_library_plan,
    route_after_masking_plan,
    route_after_repeatmasker_collect,
    route_after_resolve,
    route_after_select,
)


def build_stage() -> CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]:
    """Build the repeat annotation planning subgraph."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_repeat_annotation)
    graph.add_node("resolve_repeat_inputs", resolve_repeat_inputs)
    graph.add_node("select_repeat_strategy", select_repeat_strategy)
    graph.add_node("plan_repeat_library", plan_repeat_library)
    graph.add_node("plan_repeat_masking", plan_repeat_masking)
    graph.add_node("repeatmasker", build_repeatmasker_graph())
    graph.add_node("block", block_mask_review)
    graph.add_node("commit", commit_repeat_annotation)
    graph.add_node("fail", fail_repeat_annotation)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_repeat_inputs")
    graph.add_conditional_edges(
        "resolve_repeat_inputs",
        route_after_resolve,
        {"select": "select_repeat_strategy", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "select_repeat_strategy",
        route_after_select,
        {"library": "plan_repeat_library", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_repeat_library",
        route_after_library_plan,
        {"masking": "plan_repeat_masking", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_repeat_masking",
        route_after_masking_plan,
        {"repeatmasker": "repeatmasker", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "repeatmasker",
        route_after_repeatmasker_collect,
        {"block": "block", "fail": "fail"},
    )
    graph.add_edge("block", END)
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
