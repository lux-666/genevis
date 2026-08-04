"""Typed checkpoint boundary for the genome-survey stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, TypeAlias

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypeAliasType, TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)

if TYPE_CHECKING:
    CursorJsonValue: TypeAlias = (
        None
        | bool
        | int
        | float
        | str
        | list["CursorJsonValue"]
        | dict[str, "CursorJsonValue"]
    )
else:
    CursorJsonValue = TypeAliasType(
        "CursorJsonValue",
        None
        | bool
        | int
        | float
        | str
        | list["CursorJsonValue"]
        | dict[str, "CursorJsonValue"],
    )

EstimateValue = int | float | None
EstimateSource = Literal["provided", "survey"]
SurveyInputFamily = Literal[
    "short_reads",
    "long_reads",
]


class SurveyAssetCursor(TypedDict, total=False):
    """Compact input asset retained while the survey method runs."""

    asset_id: str
    asset_type: str
    path: str
    platform: str | None
    format: str | None
    source: str
    read_artifact_ref: ArtifactRefJson


class ResolvedSurveyAssetsCursor(TypedDict, total=False):
    short_reads: list[SurveyAssetCursor]
    long_reads: list[SurveyAssetCursor]


class SurveyParametersCursor(TypedDict, total=False):
    kmer_size: int
    hash_size: str
    threads: int


class SurveySubstepCursor(TypedDict, total=False):
    name: str
    status: str
    method_name: str
    kmer_db_ref: ArtifactRefJson
    kmer_hist_ref: ArtifactRefJson
    summary_ref: str


@with_config(ConfigDict(extra="forbid"))
class GenomeSurveyCursor(TypedDict, total=False):
    """All JSON-only keys owned by ``stage_outputs.genome_survey``."""

    status: str
    reason: str
    failure_kind: str
    error_code: str
    execution_mode: str
    env: dict[str, str]
    env_target: str
    missing_tools: list[str]
    method_name: str
    execution_attempts: list[dict[str, CursorJsonValue]]

    resolved_assets: ResolvedSurveyAssetsCursor
    selected_mode: Literal["full", "skipped_no_usable_reads"]
    survey_input_family: SurveyInputFamily
    survey_input_refs: list[str]

    mode: Literal["full", "skipped"]
    confidence: Literal["high", "low", "none"]
    candidate_methods: list[str]
    executed_methods: list[str]
    survey_parameters: SurveyParametersCursor
    substeps: dict[str, SurveySubstepCursor]
    skip_reason: str | None

    kmer_db_path: str
    kmer_hist_path: str
    kmer_db_artifact_ref: ArtifactRefJson
    kmer_hist_artifact_ref: ArtifactRefJson
    genomescope2_summary_path: str
    genomescope2_summary_ref: str
    genomescope2_output_dir: str
    genomescope2_output_dir_ref: str

    estimates: dict[str, EstimateValue]
    measured_estimates: dict[str, EstimateValue]
    provided_overrides: dict[str, EstimateValue]
    estimate_sources: dict[str, EstimateSource]
    estimate_source_paths: dict[str, str]
    genome_size_prior: int | None
    genome_size_source: EstimateSource | None
    genome_size_source_path: str | None
    notes: list[str]

    result_artifact_ref: str
    artifact_refs: list[str]


GENOME_SURVEY_CURSOR_ADAPTER = TypeAdapter(GenomeSurveyCursor)


def genome_survey_output(state: TopLevelState) -> GenomeSurveyCursor:
    """Read and validate the genome-survey cursor at its package boundary."""

    return validated_stage_output(
        state,
        "genome_survey",
        GENOME_SURVEY_CURSOR_ADAPTER,
    )


def update_genome_survey_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Merge, validate, and JSON-serialize a genome-survey cursor update."""

    return update_validated_stage_output(
        state,
        "genome_survey",
        payload,
        GENOME_SURVEY_CURSOR_ADAPTER,
    )
