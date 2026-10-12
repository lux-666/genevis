"""Typed T2T execution inputs loaded from the approved immutable chain."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domains.parameter_recommendation.resolution import render_stage_values
from app.execution.parameter_attempts import current_parameter_attempt
from app.schemas.artifacts import T2T_PARAMETER_FROZEN_PARAMETERS, ArtifactRef
from app.schemas.parameter_recommendation import (
    ResolvedParameterSources,
    T2TRecommendationContext,
)
from app.schemas.state import TopLevelState
from app.schemas.t2t import T2TThresholds
from app.stages.t2t.parameters.catalog import T2T_METHOD
from app.stages.t2t.parameters.policy import baseline_request, baseline_response
from app.stages.t2t.parameters.storage import (
    read_artifact as read_t2t_parameter_artifact,
)
from app.stages.t2t.state import T2TStageCursor, read_t2t_stage_output
from app.storage.parameter_evidence import (
    ApprovedParameterBundleV3,
    load_approved_parameter_chain_v3,
)


class T2TLongReadInput(BaseModel):
    """One canonical long-read input frozen for the T2T executor."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    member_id: str = Field(min_length=1)
    source_asset_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    read_artifact_ref: ArtifactRef | None = None


class T2TFrozenExecutionInputs(BaseModel):
    """Executor-visible values recovered from one approved T2T attempt."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    thresholds: T2TThresholds
    resolved_telomere_motif: str
    motif_source: Literal["provided_species_fact", "llm_species_resolution"]
    mode: Literal["propose_only", "manual_review", "auto_patch"]
    input_assembly_ref: str = Field(min_length=1)
    long_read_inputs: tuple[T2TLongReadInput, ...] = ()
    chromosome_list_ref: str | None = None
    chromosome_names: tuple[str, ...] = ()
    work_dir: str = Field(min_length=1)
    execution_enabled: bool

    @field_validator("long_read_inputs", mode="before")
    @classmethod
    def _normalize_long_read_inputs(cls, value: Any) -> tuple[Any, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("long_read_inputs must be a list")
        return tuple(value)

    @field_validator("chromosome_names", mode="before")
    @classmethod
    def _normalize_chromosome_names(cls, value: Any) -> tuple[Any, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("chromosome_names must be a list")
        return tuple(value)

    @model_validator(mode="after")
    def _validate_chromosome_snapshot(self) -> "T2TFrozenExecutionInputs":
        if self.chromosome_list_ref is None:
            if self.chromosome_names:
                raise ValueError(
                    "chromosome_names require a configured chromosome_list_ref"
                )
            return self
        if not self.chromosome_names:
            raise ValueError(
                "configured chromosome_list_ref requires a non-empty frozen "
                "chromosome_names snapshot"
            )
        if len(self.chromosome_names) != len(set(self.chromosome_names)):
            raise ValueError("frozen chromosome_names contain duplicates")
        return self


def load_chromosome_names_snapshot(path_value: str | None) -> tuple[str, ...]:
    """Load one configured chromosome list into a canonical immutable snapshot."""

    if path_value is None:
        return ()
    path = Path(path_value)
    if not path.is_file():
        raise ValueError(f"configured chromosome_list_ref is not a file: {path}")
    names = {
        stripped
        for line in path.read_text(encoding="utf-8").splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    }
    if not names:
        raise ValueError(
            f"configured chromosome_list_ref contains no chromosome names: {path}"
        )
    return tuple(sorted(names))


def load_t2t_baseline_bundle(
    state: TopLevelState, *, output: T2TStageCursor
) -> ApprovedParameterBundleV3:
    """Validate the policy-only schema-3 execution contract."""

    if output.get("recommendation_mode") != "deterministic_baseline":
        raise ValueError("T2T schema-3 parameters require deterministic_baseline")
    bundle = load_approved_parameter_chain_v3(
        state,
        attempt=current_parameter_attempt(output, method_id=T2T_METHOD, label="T2T"),
        expected_stage="t2t",
        expected_method_id=T2T_METHOD,
    )
    chain = bundle.chain
    decision = bundle.approval_decision["decision"]
    automatic_resolution = decision.get("automatic_resolution")
    if (
        chain.manifest.summary.get("policy") != "deterministic_baseline"
        or chain.manifest.summary.get("model_invoked") is not False
        or chain.model_request
        != baseline_request(
            chain.manifest.refs.input_snapshot_ref,
            chain.manifest.refs.basis_snapshot_ref,
        )
        or chain.model_response != baseline_response()
        or bundle.approved_l2
        or decision.get("decided_by") != "auto_rule"
        or not isinstance(automatic_resolution, dict)
        or automatic_resolution.get("source") != "policy"
    ):
        raise ValueError(
            "T2T schema-3 evidence requires empty-L2 baseline policy approval"
        )
    context = chain.input_snapshot.stage_context
    if (
        not isinstance(context, T2TRecommendationContext)
        or context.motif_source != "provided_species_fact"
        or context.resolved_telomere_motif
        != chain.input_snapshot.facts.species.supplied_telomere_motif
    ):
        raise ValueError(
            "T2T deterministic_baseline requires a supplied telomere motif"
        )
    return bundle


def load_t2t_frozen_execution_inputs(
    state: TopLevelState,
    *,
    output: T2TStageCursor | None = None,
) -> T2TFrozenExecutionInputs:
    """Load execution values without consulting mutable checkpoint copies."""

    if output is None:
        output = read_t2t_stage_output(state)
    if output.get("finishing"):
        from app.stages.finishing.advisor import load_json, loop, settings, source
        from app.stages.finishing.review import approved_proposal

        f = loop(state, "t2t")
        if f.get("execution_context"):
            f = {**f, **f["execution_context"]}
            state = {
                **state,
                "stage_outputs": {
                    **state["stage_outputs"],
                    "t2t": cast(Any, {**dict(output), "finishing": f}),
                },
            }
        approved = approved_proposal(state, "t2t", before_execution=False)
        frozen = load_json(state, f["frozen_ref"])
        inputs = T2TFrozenExecutionInputs.model_validate(frozen["execution_inputs"])
        initial = settings(state, "t2t")
        from app.stages.base import stage_work_dir

        expected_work = stage_work_dir(
            state, "t2t", "attempts", f"{f['attempt']:03d}", "t2t_finishing"
        )
        if (
            inputs.resolved_telomere_motif != initial["resolved_telomere_motif"]
            or inputs.motif_source != initial["resolved_telomere_motif_source"]
            or inputs.work_dir != str(expected_work)
            or [
                item.model_dump(mode="json", exclude_none=True)
                for item in inputs.long_read_inputs
            ]
            != initial.get("long_read_inputs", [])
            or inputs.chromosome_list_ref != initial.get("chromosome_list_ref")
        ):
            raise ValueError("T2T frozen inputs differ from original settings")
        if (
            frozen["decision_ref"] != f["decision_ref"]
            or frozen["recommendation_ref"] != f["recommendation_ref"]
            or frozen["source_ref"] != f["source_ref"]
            or approved.parameters is None
            or inputs.thresholds.model_dump(mode="json")
            != approved.parameters.model_dump(mode="json")
            or inputs.input_assembly_ref
            != str(source(state, "t2t").primary_output_path)
            or inputs.mode != "auto_patch"
        ):
            raise ValueError("T2T adviser frozen execution input binding mismatch")
        return inputs
    attempt = current_parameter_attempt(
        output,
        method_id=T2T_METHOD,
        label="T2T",
    )
    if attempt.frozen_parameters_ref is None:
        raise ValueError("T2T parameter attempt has no frozen parameter evidence")

    bundle = load_t2t_baseline_bundle(state, output=output)
    frozen = read_t2t_parameter_artifact(
        state,
        attempt.frozen_parameters_ref,
        expected_type=T2T_PARAMETER_FROZEN_PARAMETERS,
    )
    if (
        frozen.get("schema_version") != 3
        or frozen.get("stage") != "t2t"
        or frozen.get("method_id") != T2T_METHOD
    ):
        raise ValueError("T2T frozen parameter evidence has the wrong identity")

    try:
        frozen_manifest_ref = ArtifactRef.model_validate(
            frozen.get("recommendation_manifest_ref")
        )
        frozen_approval_ref = ArtifactRef.model_validate(
            frozen.get("approval_decision_ref")
        )
    except ValueError as exc:
        raise ValueError(
            "T2T frozen parameter evidence has invalid chain refs"
        ) from exc
    if (
        frozen_manifest_ref != attempt.recommendation_manifest_ref
        or frozen_approval_ref != attempt.approval_decision_ref
    ):
        raise ValueError("T2T frozen parameter evidence is not bound to the attempt")

    resolved = ResolvedParameterSources.model_validate(
        {name: frozen.get(name) for name in ResolvedParameterSources.model_fields}
    )
    if resolved.l2_approved != bundle.approved_l2:
        raise ValueError(
            "T2T frozen approved parameters do not match the recommendation"
        )
    rendered_stage_values = render_stage_values(resolved, bundle.chain.catalog)
    rendered_mode = rendered_stage_values.pop("mode", None)
    thresholds = T2TThresholds.model_validate(frozen.get("effective_thresholds"))

    context = bundle.chain.input_snapshot.stage_context
    if not isinstance(context, T2TRecommendationContext):
        raise ValueError("approved snapshot does not contain T2T context")
    execution_inputs = T2TFrozenExecutionInputs.model_validate(
        frozen.get("execution_inputs")
    )
    if execution_inputs.thresholds != thresholds:
        raise ValueError("T2T frozen execution thresholds do not match frozen evidence")
    if rendered_stage_values != thresholds.model_dump(mode="json"):
        raise ValueError(
            "T2T frozen thresholds do not match resolved parameter sources"
        )
    if execution_inputs.mode != rendered_mode:
        raise ValueError("T2T frozen mode does not match resolved parameter sources")
    if (
        execution_inputs.resolved_telomere_motif != context.resolved_telomere_motif
        or execution_inputs.motif_source != context.motif_source
    ):
        raise ValueError("T2T frozen motif does not match the approved snapshot")
    if len(execution_inputs.long_read_inputs) != context.long_read_asset_count:
        raise ValueError(
            "T2T frozen long-read inputs do not match the approved snapshot"
        )
    return execution_inputs
