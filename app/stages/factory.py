"""Factory for placeholder stage subgraphs."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.base import commit_stage, enter_stage, plan_stage


def build_placeholder_stage(stage: str):
    """Build a side-effect-free stage subgraph.

    Real method-family subgraphs will replace this enter/plan/commit skeleton
    stage by stage.
    """

    def enter(state: TopLevelState) -> dict:
        return enter_stage(stage, state)

    def plan(state: TopLevelState) -> dict:
        return plan_stage(stage, state)

    def commit(state: TopLevelState) -> dict:
        return commit_stage(stage, state)

    graph = StateGraph(TopLevelState)
    graph.add_node("enter", enter)
    graph.add_node("plan", plan)
    graph.add_node("commit", commit)
    graph.add_edge(START, "enter")
    graph.add_edge("enter", "plan")
    graph.add_edge("plan", "commit")
    graph.add_edge("commit", END)
    return graph.compile()
