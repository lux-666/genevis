"""Shared descriptor-backed parameter-catalog loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.configuration import descriptors_root, load_descriptor_registry
from app.schemas.parameter_catalog import MethodParameterCatalog


def catalog_id_for_method(method_id: str) -> str:
    """Return the short catalog directory identity for a stable method ID."""

    registry = load_descriptor_registry()
    descriptor = registry.methods.get(method_id)
    if descriptor is None:
        raise ValueError(f"Unknown method descriptor: {method_id}")
    if not descriptor.parameter_catalog:
        raise ValueError(
            f"Method descriptor {method_id!r} does not declare parameter_catalog"
        )
    return descriptor.parameter_catalog


def parameter_catalog_path(method_id: str) -> Path:
    """Resolve one catalog path from the method descriptor's stage and short ID."""

    registry = load_descriptor_registry()
    descriptor = registry.methods.get(method_id)
    if descriptor is None:
        raise ValueError(f"Unknown method descriptor: {method_id}")
    catalog_id = catalog_id_for_method(method_id)
    return (
        descriptors_root()
        / "knowledge_packs"
        / descriptor.stage
        / "methods"
        / catalog_id
        / "params.yaml"
    )


def load_method_parameter_catalog(method_id: str) -> MethodParameterCatalog:
    """Load one strict catalog and bind both identities to its descriptor."""

    path = parameter_catalog_path(method_id)
    if not path.is_file():
        raise FileNotFoundError(f"Parameter catalog not found: {path}")
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    catalog = MethodParameterCatalog.model_validate(raw)
    expected_catalog_id = catalog_id_for_method(method_id)
    if catalog.catalog_id != expected_catalog_id:
        raise ValueError(
            "Parameter catalog catalog_id mismatch: "
            f"expected {expected_catalog_id!r}, found {catalog.catalog_id!r}"
        )
    if catalog.method_id != method_id:
        raise ValueError(
            "Parameter catalog method_id mismatch: "
            f"expected {method_id!r}, found {catalog.method_id!r}"
        )
    return catalog
