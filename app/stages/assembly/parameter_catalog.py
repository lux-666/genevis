"""Load assembly method parameter catalogs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.parameter_catalog import MethodParameterCatalog

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = _PROJECT_ROOT / "descriptors" / "knowledge_packs" / "assembly" / "methods"
_REGISTRY_PATH = _PROJECT_ROOT / "descriptors" / "registry.yaml"


def catalog_id_for_method(method_id: str) -> str:
    """Return the catalog directory id for a registry method or catalog id."""

    if "." not in method_id:
        return method_id
    registry = DescriptorRegistry.load(_REGISTRY_PATH)
    descriptor = registry.methods.get(method_id)
    if descriptor is None:
        raise ValueError(f"Unknown method descriptor: {method_id}")
    if not descriptor.parameter_catalog:
        raise ValueError(f"Method descriptor {method_id!r} does not declare parameter_catalog")
    return descriptor.parameter_catalog


def parameter_catalog_path(method_id: str) -> Path:
    """Return the params.yaml path for a registry method or catalog id."""

    return CATALOG_ROOT / catalog_id_for_method(method_id) / "params.yaml"


def load_parameter_catalog(method_id: str) -> MethodParameterCatalog:
    """Load and validate a three-level method parameter catalog."""

    path = parameter_catalog_path(method_id)
    if not path.exists():
        raise FileNotFoundError(f"Parameter catalog not found: {path}")
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    catalog = MethodParameterCatalog.model_validate(raw)
    expected_id = catalog_id_for_method(method_id)
    if catalog.method_id != expected_id:
        raise ValueError(f"Parameter catalog id mismatch: expected {expected_id!r}, found {catalog.method_id!r}")
    return catalog
