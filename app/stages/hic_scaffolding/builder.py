"""Hi-C scaffolding stage builder."""

from __future__ import annotations

from app.stages.factory import build_placeholder_stage


def build_stage():
    """Build the Hi-C scaffolding placeholder subgraph."""
    return build_placeholder_stage("hic_scaffolding")
