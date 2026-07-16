"""Load the AutoHiC parameter catalog used by the Hi-C recommender."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.schemas.parameter_catalog import MethodParameterCatalog


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
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
    """Return the validated one-parameter AutoHiC recommendation catalog."""

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
    if adjustable != {"restriction_enzyme"}:
        raise ValueError(
            "AutoHiC parameter catalog must expose only restriction_enzyme at L2"
        )
    return catalog
