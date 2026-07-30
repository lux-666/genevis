"""Load the AutoHiC parameter catalog used by the Hi-C recommender."""

from __future__ import annotations

from app.domains.parameter_recommendation.catalog import load_method_parameter_catalog
from app.schemas.parameter_catalog import MethodParameterCatalog

AUTOHIC_METHOD = "hic_scaffolding.candidate.autohic"


def load_autohic_parameter_catalog() -> MethodParameterCatalog:
    """Return the validated sparse AutoHiC recommendation catalog."""

    catalog = load_method_parameter_catalog(AUTOHIC_METHOD)
    adjustable = {
        name
        for name, specification in catalog.parameters.items()
        if specification.layers.l2 is not None
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
            "AutoHiC parameter catalog does not match its declared L2 surface"
        )
    return catalog
