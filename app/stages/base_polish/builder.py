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


def build_stage() -> CompiledMethodGraph:
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
