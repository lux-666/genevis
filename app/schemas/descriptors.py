"""Descriptor and frozen attempt schemas."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from app.schemas.execution import InputDependency


class ResourceHints(BaseModel):
    """Execution resource hints from package and method descriptors."""

    cpu_cores: int = 1
    memory_gb: float = 1.0
    gpu_required: bool = False
    walltime_hours: float = 1.0
    queue: str | None = None


class IOSpec(BaseModel):
    """Input/output descriptor for a method."""

    name: str
    type: str
    description: str
    required: bool = True


class PlannerConditionalRequirement(BaseModel):
    """Planner parameter requirements triggered by other parameter values."""

    condition: dict[str, Any] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)


class PlannerMetadata(BaseModel):
    """Planner-facing method metadata declared by method descriptors."""

    required_parameters: list[str] = Field(default_factory=list)
    conditional_required_parameters: list[PlannerConditionalRequirement] = Field(
        default_factory=list
    )
    parameter_defaults: dict[str, Any] = Field(default_factory=dict)
    selection_role: str | None = None
    display_name: str | None = None
    execution_kind: Literal["local", "planner_only"] = "local"


class PackageDescriptor(BaseModel):
    """Tool-level capability descriptor."""

    package_name: str
    version_constraint: str
    language: Literal["python", "R", "cli", "container"]
    install_channel: str
    entry_patterns: list[str]
    help_summary: str
    known_pitfalls: list[str] = Field(default_factory=list)
    resource_defaults: ResourceHints = Field(default_factory=ResourceHints)


class MethodDescriptor(BaseModel):
    """Method-level business descriptor."""

    method_name: str
    stage: str
    package_ref: str
    goal: str
    required_inputs: list[IOSpec]
    expected_outputs: list[IOSpec]
    optional_inputs: list[IOSpec] = Field(default_factory=list)
    review_rules: list[str] = Field(default_factory=list)
    human_review_triggers: list[str] = Field(default_factory=list)
    resource_hints: ResourceHints = Field(default_factory=ResourceHints)
    # Gene-annotation taxonomy / candidate-bundle contract (all optional so
    # existing descriptors stay valid). category groups methods (ab_initio,
    # homology, integration, merge); materialize_outputs lists the standardized
    # candidate files the method must produce on disk.
    category: str | None = None
    recommended_default: str | bool | None = None
    materialize_outputs: list[str] = Field(default_factory=list)
    readiness_rules: list[str] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)
    evm: dict[str, str] | None = None
    parameter_catalog: str | None = None
    planner: PlannerMetadata = Field(default_factory=PlannerMetadata)


class FrozenAttemptSpec(BaseModel):
    """Deterministic execution specification consumed by runners."""

    attempt_id: str
    session_id: str
    stage: str
    method_name: str
    script_path: Path
    working_dir: Path
    interpreter: str = "bash"
    expected_outputs: list[Path]
    env: dict[str, str] = Field(default_factory=dict)
    env_setup: str = ""
    conda_bin: str | None = None
    conda_env: str | None = None
    conda_prefix: str | None = None
    input_dependencies: list[InputDependency]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _conda_env_and_prefix_are_exclusive(self) -> "FrozenAttemptSpec":
        if self.conda_env and self.conda_prefix:
            raise ValueError(
                "FrozenAttemptSpec.conda_env and .conda_prefix are mutually exclusive"
            )
        return self


class DescriptorRegistry(BaseModel):
    """Loaded descriptor registry with validated package/method references."""

    root_dir: Path
    packages: dict[str, PackageDescriptor]
    methods: dict[str, MethodDescriptor]
    by_stage: dict[str, list[str]]

    @classmethod
    def load(cls, registry_path: Path) -> "DescriptorRegistry":
        """Load registry.yaml and all referenced descriptors."""
        registry_path = registry_path.resolve()
        root_dir = registry_path.parent
        with registry_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

        packages = _load_descriptors(
            root_dir / "packages",
            raw.get("packages") or {},
            PackageDescriptor,
        )
        methods = _load_descriptors(
            root_dir / "methods",
            raw.get("methods") or {},
            MethodDescriptor,
        )

        for method_name, descriptor in methods.items():
            if descriptor.package_ref not in packages:
                raise ValueError(
                    f"Method {method_name!r} references unknown package "
                    f"{descriptor.package_ref!r}"
                )

        by_stage = raw.get("by_stage") or {}
        for stage, method_names in by_stage.items():
            for method_name in method_names or []:
                if method_name not in methods:
                    raise ValueError(
                        f"Stage {stage!r} references unknown method {method_name!r}"
                    )
                if methods[method_name].stage != stage:
                    raise ValueError(
                        f"Method {method_name!r} is not a {stage!r} method"
                    )

        return cls(
            root_dir=root_dir, packages=packages, methods=methods, by_stage=by_stage
        )


def _load_descriptors(
    root_dir: Path, refs: dict[str, str], model: type[BaseModel]
) -> dict[str, Any]:
    loaded = {}
    for name, rel_path in refs.items():
        path = root_dir / rel_path
        with path.open("r", encoding="utf-8") as handle:
            loaded[name] = model.model_validate(yaml.safe_load(handle))
    return loaded
