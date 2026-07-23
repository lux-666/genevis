"""Strict model-facing request projection for AutoHiC parameter planning."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict

from app.schemas.artifacts import ParameterAttemptTask
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.shared_parameters import SpeciesFacts


AUTOHIC_METHOD = "hic_scaffolding.candidate.autohic"
PARAMETER_RECOMMENDATION_CONTRACT = "parameter_recommendation_v1"


class _StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AutoHiCInputFacts(_StrictRequestModel):
    """Biological/library facts currently accepted by the AutoHiC planner."""

    library_protocols: list[str]
    provided_restriction_enzyme: str | None = None
    species_facts: SpeciesFacts | None = None


class AutoHiCParameterRequest(_StrictRequestModel):
    """Exact allowlisted request DTO rendered for the AutoHiC model."""

    schema_version: Literal[2] = 2
    recommendation_contract: Literal["parameter_recommendation_v1"] = (
        PARAMETER_RECOMMENDATION_CONTRACT
    )
    task: ParameterAttemptTask = "recommend_initial"
    method_id: Literal["hic_scaffolding.candidate.autohic"] = AUTOHIC_METHOD
    input_facts: AutoHiCInputFacts
    adjustable_params: dict[str, dict[str, Any]]
    attempt_history: list[dict[str, Any]] | None = None


def build_autohic_parameter_request(
    *,
    input_profile: Mapping[str, Any],
    catalog: MethodParameterCatalog,
    task: ParameterAttemptTask = "recommend_initial",
    attempt_history: list[dict[str, Any]] | None = None,
) -> AutoHiCParameterRequest:
    """Project audit-rich stage input into the strict model-facing DTO.

    The full input profile remains persisted evidence.  Only currently accepted
    biological/library facts cross the model boundary; run identity, paths,
    artifact references, asset IDs, and unrecognized metadata are deliberately
    absent from this DTO.
    """

    if catalog.method_id != "autohic":
        raise ValueError("AutoHiC model request requires the autohic catalog")
    adjustable = {
        name: {
            key: value
            for key, value in specification.model_dump(mode="json").items()
            if key
            in {
                "value_type",
                "allowed_values",
                "min",
                "max",
                "description",
                "notes",
            }
            and value is not None
        }
        for name, specification in catalog.parameters.items()
        if specification.level == 2
    }
    if not adjustable:
        raise ValueError("AutoHiC model request requires at least one L2 parameter")

    raw_metadata = input_profile.get("hic_library_metadata") or []
    if not isinstance(raw_metadata, list):
        raise ValueError("hic_library_metadata must be a list")
    protocols: list[str] = []
    for metadata in raw_metadata:
        if not isinstance(metadata, Mapping):
            raise ValueError("each Hi-C library metadata entry must be an object")
        protocol = metadata.get("library_protocol")
        if protocol is None:
            continue
        if not isinstance(protocol, str) or not protocol.strip():
            raise ValueError("library_protocol must be a non-empty string")
        protocols.append(protocol.strip())

    provided_enzyme = input_profile.get("provided_restriction_enzyme")
    if provided_enzyme is not None:
        if not isinstance(provided_enzyme, str) or not provided_enzyme.strip():
            raise ValueError(
                "provided_restriction_enzyme must be a non-empty string"
            )
        provided_enzyme = provided_enzyme.strip()

    return AutoHiCParameterRequest(
        task=task,
        input_facts=AutoHiCInputFacts(
            library_protocols=protocols,
            provided_restriction_enzyme=provided_enzyme,
            species_facts=input_profile.get("species_facts"),
        ),
        adjustable_params=adjustable,
        attempt_history=attempt_history or None,
    )
