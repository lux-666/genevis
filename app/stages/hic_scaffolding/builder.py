"""Hi-C scaffolding stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.hic_scaffolding.nodes import (
    block_hic_scaffolding,
    collect_yahs_artifacts,
    commit_hic_scaffolding,
    enter_hic_scaffolding,
    fail_hic_scaffolding,
    freeze_yahs_attempt,
    plan_hic_scaffold,
    resolve_hic_inputs,
    run_yahs_attempt,
    select_hic_mode,
    write_hic_skip,
)
from app.stages.hic_scaffolding.routing import (
    route_after_resolve,
    route_after_scaffold_plan,
    route_after_select,
    route_after_skip,
    route_after_yahs_collect,
    route_after_yahs_freeze,
    route_after_yahs_run,
)


def build_stage():
    """Build the Hi-C scaffolding scaffold/skip subgraph."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_hic_scaffolding)
    graph.add_node("resolve_hic_inputs", resolve_hic_inputs)
    graph.add_node("select_hic_mode", select_hic_mode)
    graph.add_node("plan_hic_scaffold", plan_hic_scaffold)
    graph.add_node("freeze_yahs", freeze_yahs_attempt)
    graph.add_node("run_yahs", run_yahs_attempt)
    graph.add_node("collect_yahs", collect_yahs_artifacts)
    graph.add_node("write_hic_skip", write_hic_skip)
    graph.add_node("commit", commit_hic_scaffolding)
    graph.add_node("block", block_hic_scaffolding)
    graph.add_node("fail", fail_hic_scaffolding)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_hic_inputs")
    graph.add_conditional_edges(
        "resolve_hic_inputs",
        route_after_resolve,
        {"select": "select_hic_mode", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "select_hic_mode",
        route_after_select,
        {"scaffold": "plan_hic_scaffold", "skip": "write_hic_skip", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_hic_scaffold",
        route_after_scaffold_plan,
        {"freeze_yahs": "freeze_yahs", "block": "block", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "freeze_yahs",
        route_after_yahs_freeze,
        {"run_yahs": "run_yahs", "block": "block", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "run_yahs",
        route_after_yahs_run,
        {"collect_yahs": "collect_yahs", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "collect_yahs",
        route_after_yahs_collect,
        {"block": "block", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "write_hic_skip",
        route_after_skip,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("block", END)
    graph.add_edge("fail", END)

    return graph.compile()
