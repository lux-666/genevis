"""Replay a real assembly prompt payload through the production LLM boundary.

This probe deliberately stops before parameter approval and tool execution.  It
exists to test the unstable integration boundary -- prompt rendering, the live
model response, DTO normalization, and catalog/rule validation -- without
rerunning read QC, genome survey, or an assembler.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from app.entrypoints.environment import load_dotenv_without_override
from app.llm.config import load_llm_profile
from app.stages.assembly.decision import (
    AssemblyDecisionRequest,
    AssemblyDecisionResult,
    make_assembly_decision,
)
from app.stages.assembly.parameter_catalog import load_parameter_catalog
from app.stages.assembly.parameter_profile import flatten_profile_facts
from app.stages.assembly.parameter_validation import validate_recommended_parameters


DecisionMaker = Callable[[AssemblyDecisionRequest], AssemblyDecisionResult]


def evaluate_parameter_prompt(
    prompt_payload: dict[str, Any],
    *,
    full_profile: dict[str, Any],
    session_id: str = "parameter-llm-smoke",
    decision_maker: DecisionMaker = make_assembly_decision,
) -> dict[str, Any]:
    """Run one prompt through the same decision and validation path as assembly."""

    method_id = str(prompt_payload.get("method_id") or "").strip().rsplit(".", 1)[-1]
    if not method_id:
        raise ValueError("prompt payload is missing method_id")
    catalog = load_parameter_catalog(method_id)
    decision = decision_maker(
        AssemblyDecisionRequest(
            kind="parameter_recommendation",
            session_id=session_id,
            prompt_payload=prompt_payload,
            allow_default_on_error=False,
        )
    )

    if decision.valid:
        recommended_params = dict((decision.parsed or {}).get("params") or {})
        validation = validate_recommended_parameters(
            method_id=catalog.method_id,
            catalog=catalog,
            recommended_params=recommended_params,
            readonly_facts=flatten_profile_facts(full_profile),
            rules=prompt_payload.get("rules") or {},
            full_profile=full_profile,
        )
    else:
        validation = {
            "valid": False,
            "method_id": catalog.method_id,
            "violations": [
                {
                    "param": None,
                    "value": decision.raw_response,
                    "reason": decision.error or "parameter recommendation failed",
                    "severity": "critical",
                }
            ],
            "warnings": [],
        }

    return {
        "schema_version": 1,
        "method_id": catalog.method_id,
        "task": prompt_payload.get("task"),
        "decision": decision.model_dump(mode="json"),
        "validation": validation,
        "passed": bool(decision.valid and validation.get("valid")),
    }


def resolve_input_profile(
    prompt_path: Path,
    prompt_payload: dict[str, Any],
    explicit_ref: str | Path | None = None,
) -> Path:
    """Resolve the exact full-profile artifact associated with a prompt payload."""

    candidates: list[Path] = []
    if explicit_ref:
        candidates.append(Path(explicit_ref).expanduser())
    payload_ref = prompt_payload.get("input_profile_ref")
    if payload_ref:
        ref = Path(str(payload_ref)).expanduser()
        candidates.append(ref)
        if not ref.is_absolute():
            candidates.append(Path.cwd() / ref)
        candidates.append(prompt_path.parent / ref.name)
    candidates.append(prompt_path.parent / "input_profile.json")

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "input_profile.json not found; pass --input-profile or keep it beside prompt_payload.json"
    )


def load_replay_inputs(
    prompt_ref: str | Path,
    input_profile_ref: str | Path | None = None,
) -> tuple[Path, dict[str, Any], Path, dict[str, Any]]:
    """Load and integrity-check the two replay artifacts."""

    prompt_path = Path(prompt_ref).expanduser().resolve()
    prompt_payload = _read_json_object(prompt_path, label="prompt payload")
    profile_path = resolve_input_profile(prompt_path, prompt_payload, input_profile_ref)
    full_profile = _read_json_object(profile_path, label="input profile")

    expected_hash = prompt_payload.get("input_profile_hash")
    if expected_hash and _sha256(profile_path) != str(expected_hash):
        raise ValueError(
            f"input profile hash does not match prompt payload: {profile_path}"
        )
    return prompt_path, prompt_payload, profile_path, full_profile


def run_probe(
    *,
    prompt_ref: str | Path,
    input_profile_ref: str | Path | None,
    output_dir: str | Path,
    attempts: int,
) -> tuple[dict[str, Any], Path]:
    """Run one or more live-model attempts and persist complete replay evidence."""

    if attempts < 1:
        raise ValueError("attempts must be a positive integer")
    prompt_path, prompt_payload, profile_path, full_profile = load_replay_inputs(
        prompt_ref,
        input_profile_ref,
    )
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for index in range(attempts):
        result = evaluate_parameter_prompt(
            prompt_payload,
            full_profile=full_profile,
            session_id=f"parameter-llm-smoke-{index}",
        )
        attempt_ref = destination / f"attempt_{index}.json"
        attempt_ref.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        results.append(
            {
                "attempt": index,
                "passed": result["passed"],
                "decision_valid": result["decision"]["valid"],
                "decision_error": result["decision"]["error"],
                "catalog_valid": result["validation"]["valid"],
                "result_ref": str(attempt_ref),
            }
        )

    summary = {
        "schema_version": 1,
        "prompt_payload_ref": str(prompt_path),
        "prompt_payload_sha256": _sha256(prompt_path),
        "input_profile_ref": str(profile_path),
        "input_profile_sha256": _sha256(profile_path),
        "method_id": str(prompt_payload.get("method_id") or "").rsplit(".", 1)[-1],
        "attempt_count": attempts,
        "passed_count": sum(1 for result in results if result["passed"]),
        "failed_count": sum(1 for result in results if not result["passed"]),
        "passed": all(result["passed"] for result in results),
        "attempts": results,
    }
    summary_ref = destination / "summary.json"
    summary_ref.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary, summary_ref


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay an assembly prompt payload through the live parameter LLM boundary."
    )
    parser.add_argument("--prompt-payload", required=True)
    parser.add_argument("--input-profile")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--attempts", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        dotenv_ref = load_dotenv_without_override()
        llm_profile = load_llm_profile()
        if not llm_profile.api_key:
            raise ValueError(
                "GENE_AGENT_LLM_API_KEY is not exported and was not found in the project .env"
            )
        summary, summary_ref = run_probe(
            prompt_ref=args.prompt_payload,
            input_profile_ref=args.input_profile,
            output_dir=args.output_dir,
            attempts=args.attempts,
        )
    except Exception as exc:
        print(f"error: parameter LLM smoke setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"method_id: {summary['method_id']}")
    print(f"llm_model: {llm_profile.model}")
    print(f"llm_base_url_configured: {bool(llm_profile.base_url)}")
    print(f"dotenv_ref: {dotenv_ref or 'none (exported environment only)'}")
    print(f"attempts: {summary['attempt_count']}")
    print(f"passed: {summary['passed_count']}")
    print(f"failed: {summary['failed_count']}")
    print(f"summary_ref: {summary_ref}")
    for attempt in summary["attempts"]:
        if not attempt["passed"]:
            print(
                f"attempt_{attempt['attempt']}_error: "
                f"{attempt['decision_error'] or 'catalog validation failed'}",
                file=sys.stderr,
            )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
