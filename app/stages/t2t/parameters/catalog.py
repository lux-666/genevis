"""T2T adjustable-threshold catalog loader."""

from pathlib import Path

import yaml

from app.llm.parameters.t2t.request import T2T_METHOD
from app.schemas.parameter_catalog import MethodParameterCatalog

_CATALOG = (
    Path(__file__).resolve().parents[4]
    / "descriptors"
    / "knowledge_packs"
    / "t2t"
    / "methods"
    / "minimap2"
    / "params.yaml"
)


def load_t2t_parameter_catalog() -> MethodParameterCatalog:
    if not _CATALOG.is_file():
        raise FileNotFoundError(f"T2T parameter catalog not found: {_CATALOG}")
    catalog = MethodParameterCatalog.model_validate(
        yaml.safe_load(_CATALOG.read_text(encoding="utf-8")) or {}
    )
    if catalog.method_id != T2T_METHOD:
        raise ValueError(f"T2T parameter catalog method_id must be {T2T_METHOD!r}")
    return catalog
