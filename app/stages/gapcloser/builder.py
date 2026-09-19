"""Executable gap-closing stage with an explicit manual adoption boundary."""

from langgraph.graph import END, START, StateGraph

from app.execution.lifecycle import CompiledMethodGraph
from app.schemas.state import TopLevelState
from app.stages.gapcloser.nodes import (
    build_gapcloser_graph,
    enter_gapcloser,
    fail_gapcloser,
    finish_no_gaps,
    open_adoption_gate,
)
from app.stages.gapcloser.state import read_gapcloser_stage_output as output


def route(state: TopLevelState) -> str:
    status = output(state).get("status")
    if status in {"ready", "gap_frozen", "gap_ran"}:
        return "tgs_gapcloser"
    if status == "no_gaps":
        return "no_gaps"
    if status == "review_ready":
        return "review"
    if status == "blocked_human":
        return END
    return "fail"


def build_stage() -> CompiledMethodGraph:
    graph = StateGraph(TopLevelState)
    graph.add_node("enter", enter_gapcloser)
    graph.add_node("tgs_gapcloser", build_gapcloser_graph())
    graph.add_node("review", open_adoption_gate)
    graph.add_node("no_gaps", finish_no_gaps)
    graph.add_node("fail", fail_gapcloser)
    graph.add_edge(START, "enter")
    destinations = ["tgs_gapcloser", "review", "no_gaps", "fail", END]
    graph.add_conditional_edges("enter", route, destinations)
    graph.add_conditional_edges("tgs_gapcloser", route, destinations)
    for name in ("review", "no_gaps", "fail"):
        graph.add_edge(name, END)
    return graph.compile()
