"""Executable base-polishing stage with an explicit manual adoption boundary."""

from langgraph.graph import END, START, StateGraph

from app.execution.lifecycle import CompiledMethodGraph
from app.schemas.state import TopLevelState
from app.stages.base_polish.nodes import (
    build_base_polish_graph,
    enter_base_polish,
    fail_base_polish,
    open_adoption_gate,
)
from app.stages.base_polish.state import read_base_polish_stage_output as output


def route(state: TopLevelState) -> str:
    status = output(state).get("status")
    if status in {"ready", "polish_frozen", "polish_ran"}:
        return "racon"
    if status == "review_ready":
        return "review"
    if status == "blocked_human":
        return END
    return "fail"


def build_baseline_stage() -> CompiledMethodGraph:
    graph = StateGraph(TopLevelState)
    graph.add_node("enter", enter_base_polish)
    graph.add_node("racon", build_base_polish_graph())
    graph.add_node("review", open_adoption_gate)
    graph.add_node("fail", fail_base_polish)
    graph.add_edge(START, "enter")
    destinations = ["racon", "review", "fail", END]
    graph.add_conditional_edges("enter", route, destinations)
    graph.add_conditional_edges("racon", route, destinations)
    for name in ("review", "fail"):
        graph.add_edge(name, END)
    return graph.compile()


def build_stage() -> CompiledMethodGraph:
    from app.stages.finishing.advisor import advisor_enabled
    from app.stages.finishing.workflow import build_stage as build_advisor_stage

    graph = StateGraph(TopLevelState)
    graph.add_node("advisor", build_advisor_stage("base_polish"))
    graph.add_node("baseline", build_baseline_stage())
    graph.add_conditional_edges(
        START,
        lambda s: "advisor" if advisor_enabled(s, "base_polish") else "baseline",
        ["advisor", "baseline"],
    )
    graph.add_edge("advisor", END)
    graph.add_edge("baseline", END)
    return graph.compile()
