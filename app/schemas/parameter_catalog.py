"""Multi-layer assembly parameter catalog schemas (v3).

Replaces the singular ``level``/``source``/``source_priority``/``cli`` schema
with explicit L0/L1/L2 layer declarations and typed application bindings.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ParameterValueType = Literal["int", "float", "string", "boolean", "path", "path_list"]

# ---------------------------------------------------------------------------
# Application kinds
# ---------------------------------------------------------------------------


class CliOptionApplication(BaseModel):
    """Emit ``flag value`` in ascending ``order``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["cli_option"]
    flag: str = Field(min_length=1)
    order: int
    join: Literal["scalar", "comma", "expand"] = "scalar"


class CliSwitchApplication(BaseModel):
    """Emit the flag only when the boolean parameter value is ``True``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["cli_switch"]
    flag: str = Field(min_length=1)
    order: int


class SelectorApplication(BaseModel):
    """Non-emitting selector referenced by exactly one ``choice_option``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["selector"]


class ChoiceOptionApplication(BaseModel):
    """Map selector value to a flag and emit that flag + this value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["choice_option"]
    selector: str = Field(min_length=1)
    flags: dict[str, str]
    order: int
    join: Literal["scalar", "comma", "expand"] = "scalar"


class PositionalApplication(BaseModel):
    """Emit one scalar or expand a path list at the declared position."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["positional"]
    position: int = Field(ge=0)


class ConfigKeyApplication(BaseModel):
    """Write one key in a stage-owned typed config (e.g. AutoHiC)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["config_key"]
    key: str = Field(min_length=1)


class StageValueApplication(BaseModel):
    """Supply a typed internal execution value (e.g. T2T threshold)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["stage_value"]
    key: str = Field(min_length=1)


ParameterApplication = Annotated[
    CliOptionApplication
    | CliSwitchApplication
    | SelectorApplication
    | ChoiceOptionApplication
    | PositionalApplication
    | ConfigKeyApplication
    | StageValueApplication,
    Field(discriminator="kind"),
]

# ---------------------------------------------------------------------------
# Source layer types
# ---------------------------------------------------------------------------

ExecutorSourceKind = Literal["fixed", "runtime", "materialized_default"]

# Valid L1 fact keys (closed enum for catalog validation).
CanonicalFactKey = Literal[
    "effective_genome_size_bp",
    "species_ploidy",
    "read_n50_bp",
    "raw_coverage_x",
    "hic_provided_restriction_enzyme",
]

# Valid L1 fact transforms.
FactTransform = Literal["identity", "hifiasm_hg_size"]

# Valid executor binding keys.
ExecutorBindingKey = Literal[
    "assembly_input_reads",
    "assembly_ultralong_reads",
    "assembly_hic_r1_reads",
    "assembly_hic_r2_reads",
    "assembly_method_read_type",
    "assembly_output_dir",
    "assembly_output_prefix",
    "assembly_threads",
    "hic_threads",
    "t2t_mode",
]


class FixedExecutorSource(BaseModel):
    """Route-owned value that cannot coexist with L1 or L2."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fixed"]
    value: Any


class RuntimeExecutorSource(BaseModel):
    """Value supplied through a registered typed execution binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["runtime"]
    binding: ExecutorBindingKey
    required: bool = True
    materialized_default: Any = None

    @model_validator(mode="after")
    def validate_required_vs_default(self) -> "RuntimeExecutorSource":
        if self.required and self.materialized_default is not None:
            raise ValueError(
                "runtime.required=True and materialized_default are mutually exclusive"
            )
        return self


class MaterializedDefaultExecutorSource(BaseModel):
    """Wrapper-owned concrete default with no runtime binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["materialized_default"]
    value: Any


ExecutorSource = Annotated[
    FixedExecutorSource | RuntimeExecutorSource | MaterializedDefaultExecutorSource,
    Field(discriminator="kind"),
]


class FactSource(BaseModel):
    """L1 binding to a canonical fact key with an optional transform."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact: CanonicalFactKey
    transform: FactTransform = "identity"


class ModelSource(BaseModel):
    """L2 model-recommendation slot; ``adjustable`` must be True."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adjustable: Literal[True]


# ---------------------------------------------------------------------------
# Layer declarations
# ---------------------------------------------------------------------------


class ParameterLayers(BaseModel):
    """Explicit L0/L1/L2 layer declarations for one parameter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    l0: ExecutorSource | None = None
    l1: FactSource | None = None
    l2: ModelSource | None = None

    @model_validator(mode="after")
    def validate_at_least_one_layer(self) -> "ParameterLayers":
        if self.l0 is None and self.l1 is None and self.l2 is None:
            raise ValueError("a parameter must declare at least one layer")
        return self

    @model_validator(mode="after")
    def validate_fixed_cannot_coexist(self) -> "ParameterLayers":
        if isinstance(self.l0, FixedExecutorSource) and (
            self.l1 is not None or self.l2 is not None
        ):
            raise ValueError(
                "l0.fixed cannot coexist with L1 or L2 — a fixed value cannot "
                "be overridden"
            )
        return self


# ---------------------------------------------------------------------------
# Parameter spec and catalog
# ---------------------------------------------------------------------------


class ParameterSpec(BaseModel):
    """One execution parameter with typed application and layer declarations."""

    model_config = ConfigDict(extra="forbid")

    value_type: ParameterValueType
    application: ParameterApplication
    required_for_execution: bool = False
    layers: ParameterLayers
    allowed_values: list[Any] | None = None
    min: int | float | None = None
    max: int | float | None = None
    description: str | None = None
    notes: str | None = None


class MethodParameterCatalog(BaseModel):
    """Catalog for one executable method (Assembly, AutoHiC, T2T, …)."""

    model_config = ConfigDict(extra="forbid")

    catalog_id: str = Field(min_length=1)
    method_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    description: str | None = None
    parameters: dict[str, ParameterSpec]

    @model_validator(mode="after")
    def validate_render_order_uniqueness(self) -> "MethodParameterCatalog":
        """Reject duplicate CLI order/position and dangling selectors."""

        orders: dict[int, str] = {}
        positions: dict[int, str] = {}
        selectors: set[str] = set()
        choice_selector_refs: dict[str, str] = {}

        for name, spec in self.parameters.items():
            app = spec.application
            if isinstance(app, (CliOptionApplication, CliSwitchApplication)):
                if app.order in orders:
                    raise ValueError(
                        f"duplicate CLI order {app.order} between "
                        f"{orders[app.order]!r} and {name!r}"
                    )
                orders[app.order] = name
            elif isinstance(app, ChoiceOptionApplication):
                if app.order in orders:
                    raise ValueError(
                        f"duplicate CLI order {app.order} between "
                        f"{orders[app.order]!r} and {name!r}"
                    )
                orders[app.order] = name
                if app.selector in choice_selector_refs:
                    raise ValueError(
                        f"selector {app.selector!r} is referenced by more than one "
                        "choice_option: "
                        f"{choice_selector_refs[app.selector]!r} and {name!r}"
                    )
                choice_selector_refs[app.selector] = name
            elif isinstance(app, PositionalApplication):
                if app.position in positions:
                    raise ValueError(
                        f"duplicate positional position {app.position} between "
                        f"{positions[app.position]!r} and {name!r}"
                    )
                positions[app.position] = name
            elif isinstance(app, SelectorApplication):
                selectors.add(name)

        # Every selector must be referenced by exactly one choice_option.
        dangling = selectors - set(choice_selector_refs)
        if dangling:
            raise ValueError(
                f"selector(s) with no referencing choice_option: {sorted(dangling)}"
            )
        orphan = set(choice_selector_refs) - selectors
        if orphan:
            raise ValueError(
                f"choice_option(s) reference non-existent selectors: {sorted(orphan)}"
            )
        return self
