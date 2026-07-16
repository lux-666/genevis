"""Strict Assembly parameter-recommendation response contract.

The model returns one complete set of explicit parameter recommendations for
the current attempt.  Application code does not merge catalog defaults,
previous attempts, or policy-derived values into that set.  After syntactic
parsing the recommendation is shown to a human, and an approved recommendation
is the exact mapping consumed by command construction.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


PARAMETER_RECOMMENDATION_CONTRACT = "parameter_recommendation_v1"
_RETRY_DISPOSITIONS = {"retry", "stop", "expert_review"}


def normalize_parameter_recommendation(
    response: dict[str, Any],
    request_dto: dict[str, Any],
) -> dict[str, Any]:
    """Parse the one accepted response shape without judging parameter values."""

    if not isinstance(response, dict):
        raise ValueError("parameter recommendation response must be an object")
    if request_dto.get("recommendation_contract") != PARAMETER_RECOMMENDATION_CONTRACT:
        raise ValueError("request does not use the Assembly parameter contract")

    task = request_dto.get("task")
    allowed_fields = {"parameters", "summary"}
    if task != "recommend_initial":
        allowed_fields.add("retry_disposition")
    unexpected = sorted(set(response) - allowed_fields)
    if unexpected:
        raise ValueError(
            f"parameter recommendation contains unsupported fields: {unexpected}"
        )

    parameters = response.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("parameter recommendation requires a parameters object")
    wrapped = sorted(
        name for name, value in parameters.items() if isinstance(value, dict)
    )
    if wrapped:
        raise ValueError(
            "parameter values must be direct JSON values, not wrapper objects: "
            f"{wrapped}"
        )

    summary = response.get("summary")
    if summary is not None:
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a non-empty string when provided")
        summary = summary.strip()

    retry_disposition: str | None = None
    if task != "recommend_initial":
        retry_disposition = response.get("retry_disposition")
        if retry_disposition not in _RETRY_DISPOSITIONS:
            raise ValueError(
                "retry recommendation requires retry_disposition=retry, stop, or expert_review"
            )

    return {
        "parameters": deepcopy(parameters),
        "summary": summary,
        "retry_disposition": retry_disposition,
    }
