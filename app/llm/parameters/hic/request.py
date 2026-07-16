"""Strict model-facing request projection for AutoHiC parameter planning."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict

from app.schemas.parameter_catalog import MethodParameterCatalog


AUTOHIC_METHOD = "hic_scaffolding.candidate.autohic"
PARAMETER_RECOMMENDATION_CONTRACT = "parameter_recommendation_v1"


class _StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AutoHiCInputFacts(_StrictRequestModel):
    """Biological/library facts currently accepted by the AutoHiC planner."""

    library_protocols: list[str]
    provided_restriction_enzyme: str | None = None


class AutoHiCRestrictionEnzymeCatalog(_StrictRequestModel):
    """The L2 catalog projection for the only model-adjustable parameter."""

    level: Literal[2]
    source: Literal["parameter_recommender"]
    value_type: Literal["string"]
    allowed_values: list[str] | None = None
    description: str | None = None
    notes: str | None = None


class AutoHiCAdjustableParameters(_StrictRequestModel):
    restriction_enzyme: AutoHiCRestrictionEnzymeCatalog


class AutoHiCParameterRequest(_StrictRequestModel):
    """Exact allowlisted request DTO rendered for the AutoHiC model."""

    schema_version: Literal[2] = 2
    recommendation_contract: Literal["parameter_recommendation_v1"] = (
        PARAMETER_RECOMMENDATION_CONTRACT
    )
    task: Literal["recommend_initial"] = "recommend_initial"
    method_id: Literal["hic_scaffolding.candidate.autohic"] = AUTOHIC_METHOD
    input_facts: AutoHiCInputFacts
    adjustable_params: AutoHiCAdjustableParameters


def build_autohic_parameter_request(
    *,
    input_profile: Mapping[str, Any],
    catalog: MethodParameterCatalog,
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
        name
        for name, specification in catalog.parameters.items()
        if specification.level == 2
    }
    if adjustable != {"restriction_enzyme"}:
        raise ValueError(
            "AutoHiC model request allows only restriction_enzyme at L2"
        )

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

    specification = catalog.parameters["restriction_enzyme"]
    return AutoHiCParameterRequest(
        input_facts=AutoHiCInputFacts(
            library_protocols=protocols,
            provided_restriction_enzyme=provided_enzyme,
        ),
        adjustable_params=AutoHiCAdjustableParameters(
            restriction_enzyme=AutoHiCRestrictionEnzymeCatalog(
                level=specification.level,
                source=specification.source,
                value_type=specification.value_type,
                allowed_values=specification.allowed_values,
                description=specification.description,
                notes=specification.notes,
            )
        ),
    )
