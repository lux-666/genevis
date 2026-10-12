"""T2T baseline defaults and patch-mode catalog."""

from typing import Literal

from app.domains.parameter_recommendation.catalog import load_method_parameter_catalog
from app.schemas.parameter_catalog import (
    MethodParameterCatalog,
    RuntimeExecutorSource,
)

T2T_METHOD: Literal["t2t.alignment.minimap2"] = "t2t.alignment.minimap2"

T2TMode = Literal["propose_only", "manual_review", "auto_patch"]


def load_t2t_parameter_catalog() -> MethodParameterCatalog:
    return load_method_parameter_catalog(T2T_METHOD)


def resolve_t2t_mode(value: str | None) -> T2TMode:
    """Resolve an optional run-config mode through the catalog-owned default."""

    catalog = load_t2t_parameter_catalog()
    spec = catalog.parameters.get("mode")
    source = spec.layers.l0 if spec is not None else None
    if (
        spec is None
        or spec.value_type != "string"
        or spec.application.kind != "stage_value"
        or spec.application.key != "mode"
        or not isinstance(source, RuntimeExecutorSource)
        or source.binding != "t2t_mode"
    ):
        raise ValueError("T2T catalog has no valid mode binding")
    resolved = value if value is not None else source.materialized_default
    if resolved not in {"propose_only", "manual_review", "auto_patch"}:
        raise ValueError(f"unsupported T2T mode: {resolved!r}")
    if spec.allowed_values is None or resolved not in spec.allowed_values:
        raise ValueError(f"T2T mode is not allowed by the catalog: {resolved!r}")
    if resolved == "propose_only":
        return "propose_only"
    if resolved == "manual_review":
        return "manual_review"
    return "auto_patch"
