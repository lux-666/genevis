"""Load the AutoHiC parameter catalog used by the Hi-C recommender."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.schemas.parameter_catalog import MethodParameterCatalog


_PROJECT_ROOT = Path(__file__).resolve().parents[4]
AUTOHIC_PARAMETER_CATALOG = (
    _PROJECT_ROOT
    / "descriptors"
    / "knowledge_packs"
    / "hic_scaffolding"
    / "methods"
    / "autohic"
    / "params.yaml"
)


def load_autohic_parameter_catalog() -> MethodParameterCatalog:
    """Return the validated sparse AutoHiC recommendation catalog."""

    if not AUTOHIC_PARAMETER_CATALOG.is_file():
        raise FileNotFoundError(
            f"AutoHiC parameter catalog not found: {AUTOHIC_PARAMETER_CATALOG}"
        )
    raw: Any = yaml.safe_load(
        AUTOHIC_PARAMETER_CATALOG.read_text(encoding="utf-8")
    ) or {}
    catalog = MethodParameterCatalog.model_validate(raw)
    if catalog.method_id != "autohic":
        raise ValueError(
            "AutoHiC parameter catalog method_id must be 'autohic'"
        )
    adjustable = {
        name for name, specification in catalog.parameters.items()
        if specification.level == 2
    }
    expected = {
        "restriction_enzyme",
        "number_of_edit_rounds",
        "translocation_adjust",
        "inversion_adjust",
        "debris_adjust",
        "error_max_epoch",
        "error_min_len",
        "error_max_len",
        "error_filter_score",
        "error_filter_iou_score",
    }
    if adjustable != expected:
        raise ValueError(
            "AutoHiC parameter catalog does not match the Phase 13 L2 surface"
        )
    return catalog
