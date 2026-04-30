"""Read QC stage builder."""

from __future__ import annotations

from app.stages.factory import build_placeholder_stage


def build_stage():
    """Build the read QC placeholder subgraph."""
    return build_placeholder_stage("read_qc")
