"""Inventory stage routing.

Decides whether inventory succeeds, fails, or requires human review.
"""

from __future__ import annotations

from app.schemas.state import TopLevelState


def route_from_inventory(state: TopLevelState) -> str:
    """Return the next node after inventory validation.

    Routes:
    - "register"  : validation passed with no warnings
    - "review"    : ambiguous assets or warnings that need human decision
    - "fail"      : validation errors
    """
    inventory_out = (state.get("stage_outputs") or {}).get("inventory", {})
    status = inventory_out.get("status")

    if status == "failed":
        return "fail"

    if status == "validated":
        warnings = inventory_out.get("warnings", [])
        if warnings:
            return "review"
        return "register"

    return "fail"
