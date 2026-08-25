"""Replay one persisted Assembly parameter request through the live LLM boundary.

The probe deliberately stops before human approval and execution.  It exists
to test the integration boundary that cannot be exercised by unit fixtures:
prompt rendering, the configured model, strict response parsing, catalog
membership, and typed value validation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from app.domains.parameter_recommendation.resolution import resolve_parameters
from app.entrypoints.environment import load_dotenv_without_override
from app.llm.config import load_llm_profile
from app.llm.parameters.assembly.recommender import (
    AssemblyParameterDecision,
    recommend_assembly_parameters,
)
from app.schemas.artifacts import ASSEMBLY_PARAMETER_MODEL_REQUEST
from app.schemas.parameter_recommendation import (
    RecommendationDependencyRefs,
    RecommendationInputSnapshotV3,
)
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.storage.parameter_evidence import read_parameter_json
from app.storage.paths import SessionPaths


def load_request_artifact(path: str | Path) -> dict[str, Any]:
    """Read a durable manifest or request artifact and return its exact DTO."""

    request_path = Path(path).expanduser().resolve(strict=True)
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"model request artifact is not an object: {request_path}")

    refs = payload.get("refs")
    if isinstance(refs, dict):
        paths = SessionPaths.from_run_dir(request_path.parent.parent.parent.parent)
        request_payload = read_parameter_json(
            {"data_dir": str(paths.run_dir), "session_id": paths.session_id},
            refs["model_request_ref"],
            expected_type=ASSEMBLY_PARAMETER_MODEL_REQUEST,
        )
    else:
        request_payload = payload

    messages = request_payload.get("messages")
    if (
        not isinstance(messages, list)
        or len(messages) != 2
        or any(not isinstance(item, dict) for item in messages)
    ):
        raise ValueError("model request artifact has no complete message pair")
    try:
        user_message = str(messages[1]["content"])
        request_dto = json.loads(user_message)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("model request user message is invalid") from exc
    if not isinstance(request_dto, dict):
        raise ValueError("model request DTO must be an object")
    return request_dto


def evaluate_parameter_request(
    request_dto: dict[str, Any],
    *,
    decision_maker: Callable[
        [dict[str, Any]], AssemblyParameterDecision
    ] = recommend_assembly_parameters,
) -> dict[str, Any]:
    """Run one exact request through parsing, membership, and value checks."""

    method_id = str(request_dto.get("method_id") or "")
    if not method_id:
        raise ValueError("parameter request is missing method_id")
    catalog = load_parameter_catalog(method_id)

    decision = decision_maker(request_dto)
    validation: dict[str, Any]
    if not decision.valid:
        validation = {
            "valid": False,
            "method_id": catalog.method_id,
            "violations": [
                {
                    "parameter": None,
                    "value": None,
                    "reason": decision.error or "model response was invalid",
                }
            ],
            "warnings": [],
        }
    else:
        parameters = dict((decision.parsed or {}).get("parameters") or {})
        try:
            snapshot = RecommendationInputSnapshotV3.model_validate(
                {
                    "schema_version": 3,
                    "method_id": method_id,
                    "task": request_dto.get("task", "recommend_initial"),
                    "facts": request_dto.get("facts", {}),
                    "stage_context": request_dto.get(
                        "assembly_context",
                        {
                            "stage": "assembly",
                            "assembly_route": "ont",
                            "read_type": "ont_raw",
                            "target_level": "draft",
                        },
                    ),
                    "dependency_refs": RecommendationDependencyRefs(),
                }
            )
            try:
                resolved = resolve_parameters(
                    catalog,
                    l0_executor_values={
                        "assembly_output_prefix": "diagnostic_replay",
                        "assembly_input_reads": ["/tmp/diagnostic_replay/reads.fastq"],
                    },
                    snapshot=snapshot,
                    approved_l2=parameters,
                )
            except ValueError as exc:
                validation = {
                    "valid": False,
                    "method_id": catalog.method_id,
                    "violations": [
                        {
                            "parameter": None,
                            "value": None,
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                    ],
                    "warnings": [],
                    "execution_readiness_note": (
                        "L0 executor values are not part of the persisted model "
                        "request; supply them in a full workflow execution."
                    ),
                }
                return {
                    "schema_version": 1,
                    "method_id": catalog.method_id,
                    "task": request_dto.get("task"),
                    "decision": decision.model_dump(mode="json"),
                    "validation": validation,
                    "passed": False,
                }
            validation = {
                "valid": True,
                "method_id": catalog.method_id,
                "violations": [],
                "warnings": [],
                "effective_parameters": resolved.effective_parameters,
                "effective_sources": resolved.effective_source_by_parameter,
            }
        except ValueError as exc:
            validation = {
                "valid": False,
                "method_id": catalog.method_id,
                "violations": [
                    {
                        "parameter": None,
                        "value": None,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                ],
                "warnings": [],
            }
        else:
            if resolved is None:
                validation = {
                    "valid": False,
                    "method_id": catalog.method_id,
                    "violations": [
                        {
                            "parameter": None,
                            "value": None,
                            "reason": "resolution missing",
                        }
                    ],
                    "warnings": [],
                }

    return {
        "schema_version": 1,
        "method_id": catalog.method_id,
        "task": request_dto.get("task"),
        "decision": decision.model_dump(mode="json"),
        "validation": validation,
        "passed": bool(decision.valid and validation["valid"]),
    }


def run_probe(
    *,
    model_request_ref: str | Path,
    output_dir: str | Path,
) -> tuple[dict[str, Any], Path]:
    """Run one live attempt and persist the full replay result."""

    request_path = Path(model_request_ref).expanduser().resolve(strict=True)
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    request_dto = load_request_artifact(request_path)
    result = evaluate_parameter_request(request_dto)
    result["source_model_request"] = str(request_path)
    summary_path = destination / "summary.json"
    summary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result, summary_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay one persisted Assembly parameter model request through "
            "the configured LLM."
        )
    )
    parser.add_argument("--model-request", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        dotenv_ref = load_dotenv_without_override()
        llm_profile = load_llm_profile()
        if not llm_profile.api_key:
            raise ValueError(
                "GENE_AGENT_LLM_API_KEY was neither exported nor found in .env"
            )
        summary, summary_ref = run_probe(
            model_request_ref=args.model_request,
            output_dir=args.output_dir,
        )
    except Exception as exc:
        print(
            f"error: parameter LLM smoke setup failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    print(f"method_id: {summary['method_id']}")
    print(f"llm_model: {llm_profile.model}")
    print(f"llm_base_url_configured: {bool(llm_profile.base_url)}")
    print(f"dotenv_ref: {dotenv_ref or 'none (exported environment only)'}")
    print(f"passed: {summary['passed']}")
    print(f"summary_ref: {summary_ref}")
    if not summary["passed"]:
        for violation in summary["validation"]["violations"]:
            reason = violation.get("reason") or "validation failed"
            print(f"violation: {reason}", file=sys.stderr)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
