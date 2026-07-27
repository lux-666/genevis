"""Load assembly method parameter catalogs."""

from __future__ import annotations

from pathlib import Path

from app.configuration import load_descriptor_registry
from app.domains.parameter_recommendation.catalog import (
    catalog_id_for_method as _catalog_id_for_method,
)
from app.domains.parameter_recommendation.catalog import (
    load_method_parameter_catalog,
)
from app.domains.parameter_recommendation.catalog import (
    parameter_catalog_path as _parameter_catalog_path,
)
from app.schemas.parameter_catalog import MethodParameterCatalog


def catalog_id_for_method(method_id: str) -> str:
    """Return the catalog directory ID for one stable registry method ID."""

    return _catalog_id_for_method(method_id)


def parameter_catalog_path(method_id: str) -> Path:
    """Return the params.yaml path for one stable registry method ID."""

    return _parameter_catalog_path(method_id)


def load_parameter_catalog(method_id: str) -> MethodParameterCatalog:
    """Load and validate a v3 method parameter catalog."""

    return load_method_parameter_catalog(method_id)


def lint_parameter_catalogs() -> None:
    """Load every checked-in assembly catalog and validate against the v3 schema."""

    registry = load_descriptor_registry()
    for method_id in sorted(registry.by_stage.get("assembly", [])):
        if registry.methods[method_id].parameter_catalog:
            load_parameter_catalog(method_id)
