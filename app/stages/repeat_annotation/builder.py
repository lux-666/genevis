"""Repeat annotation stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.repeat_annotation.nodes import (
    collect_repeatmasker_artifacts,
    commit_repeat_annotation,
    enter_repeat_annotation,
    fail_repeat_annotation,
    freeze_repeatmasker_attempt,
    plan_repeat_library,
    plan_repeat_masking,
    resolve_repeat_inputs,
    run_repeatmasker_attempt,
    select_repeat_strategy,
)
from app.stages.repeat_annotation.routing import (
    route_after_library_plan,
    route_after_masking_plan,
    route_after_repeatmasker_collect,
    route_after_repeatmasker_freeze,
    route_after_repeatmasker_run,
    route_after_resolve,
    route_after_select,
)


def build_stage():
    """Build the repeat annotation planning subgraph."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_repeat_annotation)
    graph.add_node("resolve_repeat_inputs", resolve_repeat_inputs)
    graph.add_node("select_repeat_strategy", select_repeat_strategy)
    graph.add_node("plan_repeat_library", plan_repeat_library)
    graph.add_node("plan_repeat_masking", plan_repeat_masking)
    graph.add_node("freeze_repeatmasker", freeze_repeatmasker_attempt)
    graph.add_node("run_repeatmasker", run_repeatmasker_attempt)
    graph.add_node("collect_repeatmasker", collect_repeatmasker_artifacts)
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
        {"freeze_repeatmasker": "freeze_repeatmasker", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "freeze_repeatmasker",
        route_after_repeatmasker_freeze,
        {"run_repeatmasker": "run_repeatmasker", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "run_repeatmasker",
        route_after_repeatmasker_run,
        {"collect_repeatmasker": "collect_repeatmasker", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "collect_repeatmasker",
        route_after_repeatmasker_collect,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
