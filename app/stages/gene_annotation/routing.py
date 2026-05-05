"""Routing helpers for the gene annotation stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("gene_annotation") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving gene annotation inputs."""
    return "fail" if _output(state).get("status") == "failed" else "profile"


def route_after_evidence_profile(state: TopLevelState) -> str:
    """Route after selecting the evidence profile."""
    return "fail" if _output(state).get("status") == "failed" else "methods"


def route_after_method_plan(state: TopLevelState) -> str:
    """Route after planning gene annotation methods."""
    return "fail" if _output(state).get("status") == "failed" else "review"


def route_after_review_plan(state: TopLevelState) -> str:
    """Route after writing review/merge planning artifact."""
    return "fail" if _output(state).get("status") == "failed" else "commit"
