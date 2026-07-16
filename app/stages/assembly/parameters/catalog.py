"""Load assembly method parameter catalogs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.parameter_catalog import MethodParameterCatalog

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
CATALOG_ROOT = _PROJECT_ROOT / "descriptors" / "knowledge_packs" / "assembly" / "methods"
_REGISTRY_PATH = _PROJECT_ROOT / "descriptors" / "registry.yaml"

# Exact runtime producer paths accepted by ``build_parameter_sources``. Keeping
# this list closed makes catalog typos fail at load time instead of silently
# falling through to a default or missing value during execution.
SOURCE_PATH_CONTRACT = frozenset(
    {
        "assembly.flye_parameters.read_type",
        "assembly.flye_parameters.threads",
        "assembly.hifiasm_parameters.threads",
        "assembly.hifiasm_ont_parameters.threads",
        "assembly.nextdenovo_parameters.read_type",
        "assembly.nextdenovo_parameters.threads",
        "assembly.read_type_prior.flye",
        "assembly.read_type_prior.nextdenovo",
        "assembly.resolved_genome_size.value_bp",
        "execution.input_fofn",
        "execution.input_reads",
        "execution.output_dir",
        "execution.output_prefix",
        "execution.work_dir",
        "read_qc.summary.n50",
        "shared_parameters.read_type",
    }
)


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
    validate_source_path_contract(catalog)
    return catalog


def validate_source_path_contract(catalog: MethodParameterCatalog) -> None:
    """Fail closed when a catalog references a path with no typed producer."""

    invalid = [
        (parameter, path)
        for parameter, spec in catalog.parameters.items()
        for path in (spec.source_priority or [])
        if path not in SOURCE_PATH_CONTRACT
    ]
    if invalid:
        details = ", ".join(f"{parameter}: {path}" for parameter, path in invalid)
        raise ValueError(f"Parameter catalog {catalog.method_id!r} has invalid source_priority path(s): {details}")


def lint_parameter_catalogs() -> None:
    """Load every checked-in assembly catalog and validate its source paths."""

    for path in sorted(CATALOG_ROOT.glob("*/params.yaml")):
        load_parameter_catalog(path.parent.name)
