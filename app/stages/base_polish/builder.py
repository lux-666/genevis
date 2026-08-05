"""Compiled placeholder subgraph for final assembly base polishing."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.schemas.state import TopLevelState
from app.stages.base_polish.nodes import stop_unimplemented_base_polish


def build_stage() -> CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]:
    """Build the fail-closed Base Polish placeholder stage."""

    graph = StateGraph(TopLevelState)
    graph.add_node("not_implemented", stop_unimplemented_base_polish)
    graph.add_edge(START, "not_implemented")
    graph.add_edge("not_implemented", END)
    return graph.compile()
