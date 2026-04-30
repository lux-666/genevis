"""Genome survey stage builder."""

from __future__ import annotations

from app.stages.factory import build_placeholder_stage


def build_stage():
    """Build the genome survey placeholder subgraph."""
    return build_placeholder_stage("genome_survey")
