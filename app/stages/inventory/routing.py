"""Inventory stage routing.

Decides whether inventory succeeds, fails, or requires human review.
"""

from __future__ import annotations

from typing import Literal

from app.schemas.state import TopLevelState
from app.stages.inventory.state import read_inventory_stage_output
from app.workflow import is_terminal_failure


def route_from_inventory(
    state: TopLevelState,
) -> Literal["register", "review", "fail"]:
    """Return the next node after inventory validation.

    Routes:
    - "register"  : validation passed with no warnings
    - "review"    : ambiguous assets or warnings that need human decision
    - "fail"      : validation errors
    """
    inventory_out = read_inventory_stage_output(state)
    status = inventory_out.get("status")

    if is_terminal_failure(status):
        return "fail"

    if status == "validated":
        if not inventory_out.get("valid"):
            return "fail"
        warnings = inventory_out.get("warnings", [])
        if _warnings_require_human_review(warnings):
            return "review"
        return "register"

    return "fail"


def _warnings_require_human_review(warnings: list[str]) -> bool:
    """Return whether inventory warnings need an explicit human decision."""
    for warning in warnings:
        normalized = str(warning).lower()
        if "human review required" in normalized:
            return True
        if "ambiguous classification" in normalized:
            return True
    return False
