"""Inventory stage builder.

Builds a real inventory subgraph that loads the manifest, validates inputs,
registers assets, and routes to success / review / failure.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.inventory.nodes import (
    commit_inventory,
    enter_inventory,
    register_assets,
    validate_inputs,
)
from app.stages.inventory.routing import route_from_inventory


def _review_gate(state: TopLevelState) -> dict:
    """Set an active interrupt for ambiguous or questionable inventory results."""
    inventory_out = (state.get("stage_outputs") or {}).get("inventory", {})
    warnings = inventory_out.get("warnings", [])
    return {
        "active_interrupt": {
            "stage": "inventory",
            "reason": "review_required",
            "messages": warnings,
        },
        "supervision_state": "blocked_human",
    }


def _fail_inventory(state: TopLevelState) -> dict:
    """Record inventory failure and stop."""
    inventory_out = (state.get("stage_outputs") or {}).get("inventory", {})
    errors = [str(error) for error in inventory_out.get("errors", [])]
    if errors:
        reason = "inventory validation failed: " + "; ".join(errors)
    else:
        reason = inventory_out.get("reason", "inventory failed")
    return {
        "current_stage": "inventory",
        "supervision_state": "failed_terminal",
        "last_error": reason,
    }


def build_stage():
    """Build the inventory subgraph with real validation and routing."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_inventory)
    graph.add_node("validate_inputs", validate_inputs)
    graph.add_node("register_assets", register_assets)
    graph.add_node("review_gate", _review_gate)
    graph.add_node("fail", _fail_inventory)
    graph.add_node("commit", commit_inventory)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "validate_inputs")
    graph.add_conditional_edges(
        "validate_inputs",
        route_from_inventory,
        {
            "register": "register_assets",
            "review": "review_gate",
            "fail": "fail",
        },
    )
    graph.add_edge("register_assets", "commit")
    graph.add_edge("review_gate", END)
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
