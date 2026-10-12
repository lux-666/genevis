"""Canonical evidence for the deterministic T2T parameter policy."""

from typing import Any

from app.schemas.artifacts import ArtifactRef
from app.stages.t2t.parameters.catalog import T2T_METHOD


def baseline_request(
    input_snapshot_ref: ArtifactRef, basis_ref: ArtifactRef
) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "policy": "deterministic_baseline",
        "method_id": T2T_METHOD,
        "task": "recommend_initial",
        "input_snapshot_sha256": input_snapshot_ref.sha256,
        "basis_sha256": basis_ref.sha256,
    }


def baseline_response() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "policy": "deterministic_baseline",
        "model_invoked": False,
        "parameters": {},
        "summary": "Use catalog defaults with empty L2.",
    }
