"""Descriptor-derived planner method catalog."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.configuration import descriptor_registry_path, load_descriptor_registry
from app.schemas.descriptors import DescriptorRegistry, MethodDescriptor

REGISTRY_PATH = descriptor_registry_path()


class PlannerMethodCatalog(BaseModel):
    """Method metadata used by the TUI planner, derived from descriptors."""

    by_stage: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    by_method: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def methods_for_stage(self, stage: str) -> list[dict[str, Any]]:
        return list(self.by_stage.get(stage, []))

    def descriptor_for_method(self, method_name: str) -> dict[str, Any] | None:
        method = self.by_method.get(method_name)
        return dict(method) if method else None


def load_planner_method_catalog(
    registry_path: Path | None = None,
) -> PlannerMethodCatalog:
    """Load the descriptor registry and derive planner-facing method metadata."""

    registry = (
        DescriptorRegistry.load(registry_path)
        if registry_path is not None
        else load_descriptor_registry()
    )
    return build_planner_method_catalog(registry)


def build_planner_method_catalog(registry: DescriptorRegistry) -> PlannerMethodCatalog:
    """Derive selectable methods, required params, defaults, and roles."""

    by_stage: dict[str, list[dict[str, Any]]] = {}
    by_method: dict[str, dict[str, Any]] = {}
    for stage, method_names in registry.by_stage.items():
        stage_methods: list[dict[str, Any]] = []
        for method_name in method_names or []:
            descriptor = registry.methods[method_name]
            spec = _planner_method_spec(descriptor)
            stage_methods.append(spec)
            by_method[method_name] = spec
        if stage_methods:
            by_stage[stage] = stage_methods
    return PlannerMethodCatalog(by_stage=by_stage, by_method=by_method)


def _planner_method_spec(descriptor: MethodDescriptor) -> dict[str, Any]:
    planner = descriptor.planner
    spec: dict[str, Any] = {
        "method_name": descriptor.method_name,
        "stage": descriptor.stage,
        "required_parameters": list(planner.required_parameters),
        "conditional_required_parameters": [
            item.model_dump(mode="json")
            for item in planner.conditional_required_parameters
        ],
        "parameter_defaults": dict(planner.parameter_defaults),
        "execution_kind": planner.execution_kind,
    }
    if planner.selection_role:
        spec["selection_role"] = planner.selection_role
    if planner.display_name:
        spec["display_name"] = planner.display_name
    if descriptor.category:
        spec["category"] = descriptor.category
    return spec
