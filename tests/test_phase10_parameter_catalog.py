"""Phase 10 parameter catalog, validation, and command-builder tests."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.control.lifecycle import resume_review_state
from app.domains.parameter_recommendation.facts import build_canonical_facts
from app.execution.evidence import (
    load_execution_evidence,
    persist_execution_result,
    persist_frozen_attempt,
)
from app.execution.method_runner import build_frozen_attempt
from app.integrity import sha256_file
from app.review.base import ResumeReviewRequest
from app.review.persistence import persist_review_decision, persist_review_request
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    ASSEMBLY_PARAMETER_BASIS,
    ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
    ASSEMBLY_PARAMETER_INPUT_SNAPSHOT,
    ASSEMBLY_PARAMETER_MODEL_REQUEST,
    ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    ASSEMBLY_PARAMETER_RECOMMENDATION,
    ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    ASSEMBLY_PARAMETER_RESPONSE_PARSE,
    ArtifactMeta,
    ArtifactRef,
    AssemblyCandidateBundle,
    ParameterAttempt,
    ParameterRecommendationCursor,
)
from app.schemas.execution import ExecutionResult
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalGenomeFacts,
    CanonicalRecommendationFacts,
    ParameterRecommendationManifestV3,
    QuantityFact,
    QuantityOrigin,
    RecommendationInputSnapshotV3,
)
from app.schemas.state import initial_state
from app.stages.assembly.command_builder import (
    ParameterCommandError,
    parameter_sources_payload,
    render_assembly_cli,
    render_assembly_config,
    resolve_assembly_parameters,
)
from app.stages.assembly.method_application.flye import FLYE_READ_TYPE_FLAGS
from app.stages.assembly.method_application.hifiasm import freeze_hifiasm_attempt
from app.stages.assembly.method_application.hifiasm_ont import (
    freeze_hifiasm_ont_attempt,
)
from app.stages.assembly.method_application.nextdenovo import freeze_nextdenovo_attempt
from app.stages.assembly.method_application.support import (
    ASSEMBLY_EXECUTION_CONTRACT_KEY,
    build_assembly_execution_contract,
)
from app.stages.assembly.parameters.catalog import (
    lint_parameter_catalogs,
    load_parameter_catalog,
)
from app.stages.assembly.parameters.context import build_assembly_recommendation_context
from app.stages.assembly.parameters.storage import (
    read_artifact as read_json_artifact,
)
from app.stages.assembly.parameters.storage import (
    write_artifact as write_attempt_json_artifact,
)
from app.stages.assembly.parameters.storage import (
    write_artifact as write_recommendation_json_artifact,
)
from app.stages.assembly.parameters.workflow import (
    execution_retry_gate,
    recommend_assembly_parameters,
)
from app.stages.assembly.routing import route_after_method_subgraph
from app.stages.assembly_review.builder import (
    build_stage as build_assembly_review_stage,
)
from app.stages.assembly_review.nodes import (
    parameter_quality_gate as assembly_review_parameter_quality_gate,
)
from app.stages.assembly_review.routing import route_after_parameter_quality_gate
from app.storage.artifacts import (
    publish_artifact_record,
    read_artifact_records,
    resolve_artifact_ref,
)
from app.storage.parameter_evidence import (
    load_approved_parameter_chain_v3,
    load_parameter_recommendation_chain_v3,
    publish_parameter_json,
)
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeModel:
    def __init__(self, payload: dict):
        self.payload = payload
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return _FakeResponse(json.dumps(self.payload))


def _runtime_hifiasm_binary() -> str | None:
    explicit = os.environ.get("HIFIASM_BIN")
    if explicit:
        binary = explicit if os.path.sep in explicit else shutil.which(explicit)
        if binary and os.access(binary, os.X_OK):
            return binary
        raise AssertionError(
            f"HIFIASM_BIN does not point to an executable hifiasm binary: {explicit}"
        )
    return shutil.which("hifiasm")


def _extract_hifiasm_help_flags(help_text: str) -> set[str]:
    return set(re.findall(r"(?m)^\s+(-\w|--[A-Za-z0-9][A-Za-z0-9_-]*)\b", help_text))


def _parse_semver_prefix(version_text: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", version_text)
    if not match:
        raise AssertionError(f"Could not parse hifiasm version from: {version_text!r}")
    return tuple(int(part or 0) for part in match.groups())


def _project_hifiasm_min_version() -> tuple[int, int, int]:
    package_descriptor = (
        Path(__file__).resolve().parents[1]
        / "descriptors"
        / "packages"
        / "hifiasm.yaml"
    )
    descriptor_text = package_descriptor.read_text(encoding="utf-8")
    match = re.search(r'version_constraint:\s*">=([^"]+)"', descriptor_text)
    if not match:
        raise AssertionError(
            "descriptors/packages/hifiasm.yaml does not declare a >= version_constraint"
        )
    return _parse_semver_prefix(match.group(1))


def _v2_state(tmpdir: str, *, session_id: str = "s1") -> dict:
    run_dir = v2_run_dir(tmpdir, session_id)
    state = initial_state(
        session_id=session_id,
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
        workflow_steps=[
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "step_id": "step-001-read_qc",
                "stage": "read_qc",
                "allow_local_execution": True,
            },
            {
                "step_id": "step-002-genome_survey",
                "stage": "genome_survey",
                "allow_local_execution": True,
            },
            {
                "step_id": "step-003-assembly",
                "stage": "assembly",
                "allow_local_execution": True,
            },
            {
                "step_id": "step-004-assembly_review",
                "stage": "assembly_review",
                "allow_local_execution": True,
            },
        ],
    )
    _activate_step(state, 3)
    return state


def _activate_step(state: dict, index: int) -> None:
    state["workflow_step_index"] = index
    state["entered_workflow_step_index"] = index
    state["current_stage"] = state["workflow_steps"][index]["stage"]


def _ref_path(state: dict, ref: dict | str) -> Path:
    if isinstance(ref, dict):
        return SessionPaths.from_run_dir(state["data_dir"]).resolve_ref(ref["path_ref"])
    return Path(ref)


def _frozen_attempt_ref(attempt_id: int) -> ArtifactRef:
    marker = f"{attempt_id:x}"[-1]
    return ArtifactRef(
        artifact_id=f"art_{marker * 32}",
        path_ref=(
            "artifacts/steps/step-003-assembly/assembly/"
            f"attempts/{attempt_id:03d}/frozen_attempt.json"
        ),
        sha256=marker * 64,
        size_bytes=1,
        artifact_type="assembly.frozen_attempt",
    )


def _snapshot(
    method_id: str,
    *,
    genome_size: int = 2_000_000,
) -> RecommendationInputSnapshotV3:
    if method_id == "assembly.candidate.hifiasm":
        route = "hifi"
        read_type = "pacbio_hifi"
    elif method_id == "assembly.candidate.flye":
        route = "cyclone"
        read_type = "ont_hq"
    else:
        route = "ont"
        read_type = "ont_raw"
    return RecommendationInputSnapshotV3(
        schema_version=3,
        method_id=method_id,
        task="recommend_initial",
        facts=CanonicalRecommendationFacts(
            genome=CanonicalGenomeFacts(
                effective_genome_size=QuantityFact(
                    value=genome_size,
                    unit="bp",
                    origin=QuantityOrigin(kind="provided", producer="run_config"),
                )
            )
        ),
        stage_context=AssemblyRecommendationContext(
            stage="assembly",
            assembly_route=route,
            read_type=read_type,
            target_level="draft",
        ),
    )


def _l0_executor_values(method_id: str) -> dict:
    if method_id == "assembly.candidate.flye":
        return {
            "assembly_method_read_type": "nano-hq",
            "assembly_input_reads": ["/reads/test.fastq"],
            "assembly_output_dir": "/work/flye",
            "assembly_threads": 4,
        }
    if method_id == "assembly.candidate.nextdenovo":
        return {
            "assembly_method_read_type": "ont",
            "assembly_input_reads": "/work/input.fofn",
            "assembly_output_dir": "/work/nextdenovo",
            "assembly_threads": 4,
        }
    return {
        "assembly_output_prefix": "/work/hifiasm/out",
        "assembly_input_reads": ["/reads/test.fastq"],
        "assembly_threads": 4,
    }


def _persist_parameter_attempt(
    state: dict,
    *,
    method_id: str,
    recommended_params: dict | None = None,
    status: str = "recommended",
    attempt_id: int = 0,
    task: str = "recommend_initial",
    previous_attempt: dict | None = None,
    execution_result: dict | None = None,
    execution_stderr: str = "",
) -> dict:
    """Publish a valid approved v3 chain and return its compact attempt cursor."""

    if attempt_id != 0 or task != "recommend_initial" or previous_attempt is not None:
        raise AssertionError("this fixture publishes only an initial recommendation")

    params = dict(recommended_params or {})
    catalog = load_parameter_catalog(method_id)
    snapshot = _snapshot(method_id)
    common = {
        "state": state,
        "identifier": attempt_id,
        "method_name": method_id,
    }
    input_snapshot_ref = write_recommendation_json_artifact(
        **common,
        filename="input_snapshot.json",
        payload=snapshot.model_dump(mode="json"),
        artifact_type=ASSEMBLY_PARAMETER_INPUT_SNAPSHOT,
    )
    basis_ref = write_recommendation_json_artifact(
        **common,
        filename="basis_snapshot.json",
        payload={
            "schema_version": 3,
            "method_id": catalog.method_id,
            "catalog_snapshot": catalog.model_dump(mode="json"),
            "literature_selection": {},
            "literature_cases": [],
            "method_guidance": [],
        },
        artifact_type=ASSEMBLY_PARAMETER_BASIS,
    )
    model_request_ref = write_recommendation_json_artifact(
        **common,
        filename="model_request.json",
        payload={"schema_version": 3, "messages": []},
        artifact_type=ASSEMBLY_PARAMETER_MODEL_REQUEST,
    )
    model_response_ref = write_recommendation_json_artifact(
        **common,
        filename="model_response.json",
        payload={"schema_version": 2, "validity": "parsed"},
        artifact_type=ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    )
    recommendation_ref = write_recommendation_json_artifact(
        **common,
        filename="recommendation.json",
        payload={
            "schema_version": 2,
            "method_id": catalog.method_id,
            "task": task,
            "recommendation": {
                "parameters": params,
            },
        },
        artifact_type=ASSEMBLY_PARAMETER_RECOMMENDATION,
    )
    response_parse_ref = write_recommendation_json_artifact(
        **common,
        filename="response_parse.json",
        payload={"schema_version": 1, "parsed": True, "error": None},
        artifact_type=ASSEMBLY_PARAMETER_RESPONSE_PARSE,
    )
    manifest = ParameterRecommendationManifestV3(
        schema_version=3,
        recommendation_contract="parameter_recommendation_v1",
        stage="assembly",
        method_id=method_id,
        task=task,
        recommendation_id=attempt_id,
        status="pending_approval",
        refs={
            "input_snapshot_ref": input_snapshot_ref.model_dump(mode="json"),
            "basis_snapshot_ref": basis_ref.model_dump(mode="json"),
            "model_request_ref": model_request_ref.model_dump(mode="json"),
            "model_response_ref": model_response_ref.model_dump(mode="json"),
            "recommendation_ref": recommendation_ref.model_dump(mode="json"),
            "response_parse_ref": response_parse_ref.model_dump(mode="json"),
        },
        summary={"response_parsed": True},
    )
    manifest_ref = write_recommendation_json_artifact(
        **common,
        filename="recommendation_manifest.json",
        payload=manifest.model_dump(mode="json"),
        artifact_type=ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    )
    persisted_request = persist_review_request(
        state,
        stage="assembly",
        gate_id="parameter_approval",
        reason="test_parameter_approval",
        payload={
            "schema_version": 2,
            "attempt_id": attempt_id,
            "recommendation_manifest_ref": manifest_ref.model_dump(mode="json"),
            "recommendation_manifest_hash": manifest_ref.sha256,
            "decisions_allowed": ["approve_parameters", "reject_parameters"],
        },
        bindings={"attempt_id": attempt_id, "task": task},
    )
    approval_ref = persist_review_decision(
        state["data_dir"],
        session_id=state["session_id"],
        review_request_id=persisted_request.review_request_id,
        stage="assembly",
        gate_id="parameter_approval",
        decision={
            "schema_version": 2,
            "decision": "approve_parameters",
            "attempt_id": attempt_id,
            "validated_recommendation_manifest_ref": manifest_ref.model_dump(
                mode="json"
            ),
            "validated_recommendation_hash": manifest_ref.sha256,
        },
        request_ref=persisted_request.request_ref,
        request_hash=persisted_request.request_hash,
        artifact_type=ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    )
    frozen_attempt_ref = (
        _frozen_attempt_ref(attempt_id)
        if status != "recommended" and execution_result is None
        else None
    )
    execution_ref = None
    if execution_result is not None:
        paths = SessionPaths.from_run_dir(state["data_dir"])
        log_dir = paths.workflow_step_work_path(
            "step-003-assembly", "assembly", "parameter_test_logs"
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        script_path = log_dir / f"attempt_{attempt_id}.sh"
        script_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        frozen_attempt = build_frozen_attempt(
            attempt_id=(
                f"{state['session_id']}:step-003-assembly:parameter-{attempt_id}"
            ),
            session_id=state["session_id"],
            stage="assembly",
            method_name=method_id,
            script_path=script_path,
            working_dir=log_dir,
            expected_outputs=[],
            env=None,
            env_spec=None,
            metadata={"workflow_step_id": "step-003-assembly"},
        )
        frozen_attempt_ref = persist_frozen_attempt(state, frozen_attempt)
        stdout_source = log_dir / f"attempt_{attempt_id}.stdout.log"
        stderr_source = log_dir / f"attempt_{attempt_id}.stderr.log"
        stdout_source.write_text("", encoding="utf-8")
        stderr_source.write_text(execution_stderr, encoding="utf-8")
        stdout_published = publish_artifact_record(
            state["data_dir"],
            state["session_id"],
            ArtifactMeta.from_file(
                stdout_source,
                stage="assembly",
                method_name=method_id,
                artifact_type="command.stdout",
                metadata={"attempt_id": attempt_id},
            ),
            destination=paths.workflow_step_artifact_path(
                "step-003-assembly",
                "assembly",
                "logs",
                f"parameter_attempt_{attempt_id}.stdout.log",
            ),
        )
        stderr_published = publish_artifact_record(
            state["data_dir"],
            state["session_id"],
            ArtifactMeta.from_file(
                stderr_source,
                stage="assembly",
                method_name=method_id,
                artifact_type="command.stderr",
                metadata={"attempt_id": attempt_id},
            ),
            destination=paths.workflow_step_artifact_path(
                "step-003-assembly",
                "assembly",
                "logs",
                f"parameter_attempt_{attempt_id}.stderr.log",
            ),
        )
        runtime_result = ExecutionResult(
            attempt_id=frozen_attempt.attempt_id,
            success=bool(execution_result.get("success", False)),
            return_code=int(execution_result.get("return_code", 1)),
            stdout_path=stdout_published.path,
            stderr_path=stderr_published.path,
            output_paths=[],
            status=execution_result.get("status", "failed"),
            status_detail=str(execution_result.get("status_detail", "")),
        )
        execution_ref = persist_execution_result(
            state,
            stage="assembly",
            method_name=method_id,
            frozen_attempt_ref=frozen_attempt_ref,
            result=runtime_result,
            error=str(execution_result.get("error", "")),
        )
    frozen_parameters_ref = None
    if status != "recommended":
        resolved = resolve_assembly_parameters(
            catalog=catalog,
            snapshot=snapshot,
            approved_l2=params,
            l0_executor_values=_l0_executor_values(method_id),
        )
        frozen_parameters_ref = write_attempt_json_artifact(
            state,
            attempt_id,
            "frozen_parameters.json",
            {
                "schema_version": 3,
                "stage": "assembly",
                "attempt_id": attempt_id,
                "method_id": method_id,
                "recommendation_manifest_ref": manifest_ref.model_dump(mode="json"),
                "approval_decision_ref": approval_ref.model_dump(mode="json"),
                **parameter_sources_payload(resolved),
            },
            artifact_type=ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
            method_name=method_id,
            scope="attempt",
        )
    return ParameterAttempt(
        schema_version=3,
        attempt_id=attempt_id,
        method_id=method_id,
        status=status,
        task=task,
        recommendation_manifest_ref=manifest_ref,
        approval_decision_ref=approval_ref,
        frozen_parameters_ref=frozen_parameters_ref,
        frozen_attempt_ref=frozen_attempt_ref,
        execution_ref=execution_ref,
    ).model_dump(mode="json")


def _attach_frozen_parameter_sources(
    state: dict,
    attempt_value: dict,
    *,
    status: str,
) -> dict:
    attempt = ParameterAttempt.model_validate(attempt_value)
    bundle = load_approved_parameter_chain_v3(
        state,
        attempt=attempt,
        expected_stage="assembly",
        expected_method_id=attempt.method_id,
    )
    resolved = resolve_assembly_parameters(
        catalog=bundle.chain.catalog,
        snapshot=bundle.chain.input_snapshot,
        approved_l2=bundle.approved_l2,
        l0_executor_values=_l0_executor_values(attempt.method_id),
    )
    frozen_parameters_ref = write_attempt_json_artifact(
        state,
        attempt.attempt_id,
        "frozen_parameters.json",
        {
            "schema_version": 3,
            "stage": "assembly",
            "attempt_id": attempt.attempt_id,
            "method_id": attempt.method_id,
            "recommendation_manifest_ref": (
                attempt.recommendation_manifest_ref.model_dump(mode="json")
            ),
            "approval_decision_ref": attempt.approval_decision_ref.model_dump(
                mode="json"
            ),
            **parameter_sources_payload(resolved),
        },
        artifact_type=ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
        method_name=attempt.method_id,
    )
    return ParameterAttempt.model_validate(
        {
            **attempt.model_dump(mode="json"),
            "status": status,
            "frozen_parameters_ref": frozen_parameters_ref.model_dump(mode="json"),
            "frozen_attempt_ref": _frozen_attempt_ref(attempt.attempt_id).model_dump(
                mode="json"
            ),
        }
    ).model_dump(mode="json")


def _parameter_response(
    **overrides: int | bool | str,
) -> dict:
    return {
        "parameters": dict(overrides),
        "summary": "Present this parameter recommendation for human review.",
    }


def _complete_hifiasm_ont_response(
    **overrides: int | bool | str,
) -> dict:
    catalog = load_parameter_catalog("assembly.candidate.hifiasm_ont")
    l2_parameters = {
        parameter
        for parameter, spec in catalog.parameters.items()
        if spec.layers.l2 is not None
    }
    unknown = sorted(set(overrides) - l2_parameters)
    if unknown:
        raise AssertionError(f"test response contains non-L2 ONT parameters: {unknown}")
    return _parameter_response(**overrides)


class ParameterCatalogSchemaTests(unittest.TestCase):
    def test_load_hifiasm_ont_catalog(self):
        catalog = load_parameter_catalog("assembly.candidate.hifiasm_ont")

        self.assertEqual(catalog.catalog_id, "hifiasm_ont")
        self.assertEqual(catalog.method_id, "assembly.candidate.hifiasm_ont")
        self.assertEqual(catalog.tool, "hifiasm")
        for name in ("output_prefix", "threads", "input_reads", "ont_mode"):
            self.assertIsNotNone(catalog.parameters[name].layers.l0)
        self.assertTrue(catalog.parameters["ont_mode"].layers.l0.value)
        self.assertIsNotNone(catalog.parameters["hg_size"].layers.l1)
        self.assertIsNotNone(catalog.parameters["n_hap"].layers.l0)
        for name in (
            "hom_cov",
            "purge_level",
            "read_length_cutoff",
            "score_cutoff",
        ):
            self.assertIsNotNone(catalog.parameters[name].layers.l2)

    def test_flye_catalog_read_type_values_match_execution_flags(self):
        catalog = load_parameter_catalog("assembly.candidate.flye")

        self.assertEqual(
            catalog.parameters["read_type"].allowed_values, list(FLYE_READ_TYPE_FLAGS)
        )

    def test_nextdenovo_catalog_keeps_behavior_knobs_in_l2(self):
        catalog = load_parameter_catalog("assembly.candidate.nextdenovo")

        self.assertIsNotNone(catalog.parameters["read_cutoff"].layers.l2)
        self.assertIsNotNone(catalog.parameters["nextgraph_a"].layers.l2)

    def test_catalogs_cover_supported_hifiasm_cli_surface(self):
        expected_keys = {
            "output_prefix",
            "threads",
            "input_reads",
            "help",
            "version",
            "ont_mode",
            "ignore_cached_overlaps",
            "write_paf",
            "write_ec",
            "kmer_length",
            "minimizer_window",
            "bloom_filter_bits",
            "high_freq_kmer_drop",
            "max_overlaps_per_read",
            "correction_rounds",
            "adapter_trim_length",
            "max_kmer_occurrence",
            "hg_size",
            "hom_cov",
            "n_hap",
            "assembly_cleaning_rounds",
            "contig_bubble_pop_distance",
            "unitig_bubble_pop_distance",
            "short_unitig_read_count",
            "max_overlap_drop_ratio",
            "min_overlap_drop_ratio",
            "post_join",
            "low_quality_bed_threshold",
            "break_low_coverage",
            "break_high_coverage",
            "break_overlap_rate",
            "trio_lower_frequency",
            "trio_upper_frequency",
            "trio_unexpected_occurrence",
            "trio_dual",
            "purge_level",
            "purge_similarity",
            "duplicate_overlap_min_reads",
            "purge_max",
            "seed",
            "homology_base_similarity",
            "hic_weight_rounds",
            "hic_perturb_rounds",
            "hic_flip_fraction",
            "hic_misjoin_length",
            "ul_error_rate",
            "ul_tip_read_count",
            "path_max_drop_ratio",
            "path_min_drop_ratio",
            "ul_cut",
            "dual_scaffold",
            "scaffold_gap",
            "telomere_motif",
            "telomere_penalty",
            "telomere_drop",
            "telomere_min_score",
            "contig_tip_read_count",
            "chimeric_support",
            "chimeric_flank",
            "read_length_cutoff",
            "score_cutoff",
        }
        for method_id in (
            "assembly.candidate.hifiasm",
            "assembly.candidate.hifiasm_ont",
        ):
            catalog = load_parameter_catalog(method_id)
            self.assertTrue(expected_keys <= set(catalog.parameters))

    def test_cataloged_hifiasm_flags_exist_in_runtime_help_when_binary_available(self):
        binary = _runtime_hifiasm_binary()
        if not binary:
            if os.environ.get("PHASE10_REQUIRE_HIFIASM_RUNTIME") == "1":
                self.fail(
                    "PHASE10_REQUIRE_HIFIASM_RUNTIME=1 requires HIFIASM_BIN "
                    "or hifiasm on PATH."
                )
            self.skipTest(
                "Set HIFIASM_BIN or put hifiasm on PATH to verify runtime help flags."
            )

        version_result = subprocess.run(
            [binary, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        runtime_version = version_result.stdout.strip()
        self.assertGreaterEqual(
            _parse_semver_prefix(runtime_version), _project_hifiasm_min_version()
        )

        help_result = subprocess.run(
            [binary, "-h"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        help_flags = _extract_hifiasm_help_flags(help_result.stdout)
        self.assertIn("--ont", help_flags)
        self.assertIn("--rl-cut", help_flags)
        self.assertIn("--sc-cut", help_flags)

        for method_id in (
            "assembly.candidate.hifiasm",
            "assembly.candidate.hifiasm_ont",
        ):
            catalog = load_parameter_catalog(method_id)
            catalog_flags = set()
            for spec in catalog.parameters.values():
                application = spec.application
                flag = getattr(application, "flag", None)
                if flag:
                    catalog_flags.add(flag)
                catalog_flags.update(getattr(application, "flags", {}).values())
            unsupported = sorted(catalog_flags - help_flags - {"-h", "--version"})
            self.assertEqual(
                [],
                unsupported,
                (
                    f"{method_id} catalog has flags not advertised by "
                    f"{binary} {runtime_version}: {unsupported}"
                ),
            )

    def test_load_catalog_is_cwd_independent(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                os.chdir(tmpdir)
                catalog = load_parameter_catalog("assembly.candidate.hifiasm_ont")
            finally:
                os.chdir(old_cwd)

        self.assertEqual(catalog.catalog_id, "hifiasm_ont")
        self.assertEqual(catalog.method_id, "assembly.candidate.hifiasm_ont")

    def test_registry_method_catalog_id_comes_from_descriptor(self):
        self.assertEqual(
            load_parameter_catalog("assembly.candidate.hifiasm").catalog_id,
            "hifiasm",
        )
        self.assertEqual(
            load_parameter_catalog("assembly.candidate.hifiasm_ont").catalog_id,
            "hifiasm_ont",
        )

    def test_missing_catalog_raises(self):
        with self.assertRaisesRegex(ValueError, "Unknown method descriptor"):
            load_parameter_catalog("missing_method")

    def test_legacy_level_and_cli_fields_are_rejected(self):
        data = {
            "catalog_id": "bad",
            "method_id": "assembly.candidate.bad",
            "tool": "hifiasm",
            "parameters": {
                "bad_param": {
                    "cli": "--bad",
                    "level": 3,
                    "source": "parameter_recommender",
                    "value_type": "int",
                }
            },
        }
        with self.assertRaises(ValidationError):
            MethodParameterCatalog.model_validate(data)

    def test_legacy_source_priority_field_is_rejected(self):
        data = {
            "catalog_id": "bad",
            "method_id": "assembly.candidate.bad",
            "tool": "hifiasm",
            "parameters": {
                "bad_param": {
                    "value_type": "int",
                    "application": {
                        "kind": "cli_option",
                        "flag": "--bad",
                        "order": 1,
                    },
                    "layers": {"l2": {"adjustable": True}},
                    "source_priority": ["genome_survey.estimates.genome_size"],
                }
            },
        }
        with self.assertRaises(ValidationError):
            MethodParameterCatalog.model_validate(data)

    def test_loader_rejects_method_id_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "params.yaml"
            path.write_text(
                "catalog_id: hifiasm\n"
                "method_id: assembly.candidate.wrong\n"
                "tool: hifiasm\n"
                "parameters: {}\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "app.domains.parameter_recommendation.catalog.parameter_catalog_path",
                    return_value=path,
                ),
                patch(
                    "app.domains.parameter_recommendation.catalog.catalog_id_for_method",
                    return_value="hifiasm",
                ),
            ):
                with self.assertRaises(ValueError):
                    load_parameter_catalog("assembly.candidate.hifiasm")


class CanonicalFactsAndContextTests(unittest.TestCase):
    def _inputs(self):
        shared = {
            "organism": "Example species",
            "taxon_id": 12345,
            "clade": "vertebrate",
            "ploidy": 2,
        }
        read_qc = {
            "summary": {
                "num_seqs": 100,
                "total_bases": 5_000_000_000,
                "n50": 30_000,
                "avg_len": 25_000,
                "max_len": 90_000,
                "q20_percent": 91,
                "avg_qual": 22.5,
            }
        }
        genome_survey = {
            "confidence": "high",
            "estimates": {
                "genome_size": 1_200_000_000,
                "heterozygosity": 0.012,
                "repeat_fraction": 0.42,
            },
            "measured_estimates": {"genome_size": 1_000_000_000},
            "provided_overrides": {"genome_size": 1_200_000_000},
            "estimate_sources": {"genome_size": "provided"},
        }
        return shared, read_qc, genome_survey

    def test_canonical_facts_preserve_values_units_and_origins(self):
        shared, read_qc, genome_survey = self._inputs()

        facts = build_canonical_facts(
            shared_parameters=shared,
            read_qc_output=read_qc,
            genome_survey_result=genome_survey,
            asset_flags={"has_hifi": False, "has_ont": True},
        )

        self.assertEqual(facts.species.scientific_name, "Example species")
        self.assertEqual(facts.species.taxon_id, "12345")
        self.assertEqual(facts.species.ploidy.value, 2)
        self.assertEqual(facts.reads.read_n50.value, 30_000)
        self.assertEqual(facts.reads.q20_fraction.value, 0.91)
        self.assertEqual(facts.genome.effective_genome_size.value, 1_200_000_000)
        self.assertEqual(facts.genome.effective_genome_size.unit, "bp")
        self.assertEqual(facts.genome.effective_genome_size.origin.kind, "provided")
        self.assertAlmostEqual(
            facts.genome.provided_vs_measured_relative_change.value,
            0.2,
        )
        self.assertAlmostEqual(
            facts.sequencing.raw_coverage.value,
            5_000_000_000 / 1_200_000_000,
        )
        self.assertEqual(facts.sequencing.raw_coverage.origin.kind, "derived")
        self.assertEqual(
            facts.sequencing.raw_coverage.origin.producer,
            "parameter_recommendation",
        )
        self.assertTrue(facts.asset_presence.has_ont)

    def test_survey_measurement_drives_coverage_without_provided_override(self):
        shared, read_qc, genome_survey = self._inputs()
        genome_survey["estimates"]["genome_size"] = 1_000_000_000
        genome_survey["provided_overrides"] = {}
        genome_survey["estimate_sources"]["genome_size"] = "survey"

        facts = build_canonical_facts(
            shared_parameters=shared,
            read_qc_output=read_qc,
            genome_survey_result=genome_survey,
        )

        self.assertEqual(facts.sequencing.raw_coverage.value, 5.0)
        self.assertEqual(facts.genome.effective_genome_size.origin.kind, "measured")
        self.assertIsNone(facts.genome.provided_genome_size)
        self.assertIsNone(facts.genome.provided_vs_measured_relative_change)

    def test_missing_genome_size_does_not_recover_legacy_coverage(self):
        shared, read_qc, _genome_survey = self._inputs()
        read_qc["summary"]["effective_coverage_1kb"] = 99

        facts = build_canonical_facts(
            shared_parameters=shared,
            read_qc_output=read_qc,
            genome_survey_result={
                "estimates": {"heterozygosity": 0.01, "repeat_fraction": 0.3},
                "measured_estimates": {},
                "provided_overrides": {},
                "estimate_sources": {},
            },
        )

        self.assertIsNone(facts.genome.effective_genome_size)
        self.assertIsNone(facts.sequencing.raw_coverage)

    def test_context_uses_selection_and_explicit_stage_fields(self):
        assembly_out = {
            "assembly_selection": {
                "read_type": "ont_hq",
                "primary_method": "assembly.candidate.flye",
                "source": "run_config",
                "rationale": "test",
            },
            "assembly_route": "cyclone",
            "target_level": "near_t2t",
            "input_profile": {
                "recommended_route": "hifi",
                "read_type": "hifi",
                "target_level": "draft",
            },
        }

        context = build_assembly_recommendation_context(
            {},
            assembly_out=assembly_out,
        )

        self.assertEqual(context.assembly_route, "cyclone")
        self.assertEqual(context.read_type, "ont_hq")
        self.assertEqual(context.target_level, "near_t2t")

    def test_manifest_rejects_legacy_input_profile_ref(self):
        ref = ArtifactRef(
            artifact_id=f"art_{'a' * 32}",
            path_ref=(
                "artifacts/steps/step-003-assembly/assembly/"
                "parameter_recommendations/000/a.json"
            ),
            sha256="a" * 64,
            size_bytes=1,
            artifact_type="assembly.parameter.test",
        ).model_dump(mode="json")
        refs = {
            "input_snapshot_ref": ref,
            "basis_snapshot_ref": ref,
            "model_request_ref": ref,
            "model_response_ref": ref,
            "response_parse_ref": ref,
            "recommendation_ref": ref,
        }
        refs["input_profile_ref"] = refs.pop("input_snapshot_ref")

        with self.assertRaises(ValidationError):
            ParameterRecommendationManifestV3.model_validate(
                {
                    "schema_version": 3,
                    "recommendation_contract": "parameter_recommendation_v1",
                    "stage": "assembly",
                    "method_id": "assembly.candidate.hifiasm_ont",
                    "task": "recommend_initial",
                    "recommendation_id": 0,
                    "status": "pending_approval",
                    "refs": refs,
                }
            )


class CommandBuilderTests(unittest.TestCase):
    def setUp(self):
        self.method_id = "assembly.candidate.hifiasm_ont"
        self.catalog = load_parameter_catalog(self.method_id)
        self.snapshot = _snapshot(self.method_id, genome_size=1_000_000_000)

    def test_hifiasm_ont_resolver_and_renderer_preserve_source_ownership(self):
        resolved = resolve_assembly_parameters(
            catalog=self.catalog,
            snapshot=self.snapshot,
            approved_l2={
                "hom_cov": 36,
                "purge_level": 1,
                "read_length_cutoff": 3000,
                "score_cutoff": 10,
            },
            l0_executor_values={
                "assembly_output_prefix": "work/out/asm",
                "assembly_threads": 8,
                "assembly_input_reads": ["reads1.fq", "reads2.fq"],
            },
        )
        argv = render_assembly_cli(self.catalog, resolved)

        self.assertEqual(argv[0], "hifiasm")
        self.assertLess(argv.index("--ont"), argv.index("reads1.fq"))
        self.assertEqual(argv[argv.index("-o") + 1], "work/out/asm")
        self.assertEqual(argv[argv.index("-t") + 1], "8")
        self.assertEqual(argv[argv.index("--hg-size") + 1], "1g")
        self.assertEqual(argv[argv.index("--hom-cov") + 1], "36")
        self.assertEqual(argv[argv.index("-l") + 1], "1")
        self.assertEqual(argv[argv.index("--rl-cut") + 1], "3000")
        self.assertEqual(argv[-2:], ["reads1.fq", "reads2.fq"])
        self.assertEqual(resolved.l1_facts["hg_size"], "1g")
        self.assertEqual(resolved.l2_approved["purge_level"], 1)
        self.assertEqual(
            resolved.effective_source_by_parameter["purge_level"],
            "l2",
        )

    def test_resolver_rejects_unknown_approved_parameter(self):
        with self.assertRaisesRegex(ParameterCommandError, "not cataloged as L2"):
            resolve_assembly_parameters(
                catalog=self.catalog,
                snapshot=self.snapshot,
                approved_l2={"not_real": 1},
                l0_executor_values=_l0_executor_values(self.method_id),
            )

    def test_resolver_rejects_l2_override_of_l1_parameter(self):
        with self.assertRaisesRegex(ParameterCommandError, "not cataloged as L2"):
            resolve_assembly_parameters(
                catalog=self.catalog,
                snapshot=self.snapshot,
                approved_l2={"hg_size": "1g"},
                l0_executor_values=_l0_executor_values(self.method_id),
            )

    def test_resolver_rejects_unrenderable_l2_without_substituting_default(self):
        with self.assertRaisesRegex(
            ParameterCommandError, "cannot be rendered as float"
        ):
            resolve_assembly_parameters(
                catalog=self.catalog,
                snapshot=self.snapshot,
                approved_l2={"purge_similarity": "default"},
                l0_executor_values=_l0_executor_values(self.method_id),
            )

    def test_parameter_sources_payload_contains_all_six_views(self):
        resolved = resolve_assembly_parameters(
            catalog=self.catalog,
            snapshot=self.snapshot,
            approved_l2={"purge_level": 1},
            l0_executor_values={
                "assembly_output_prefix": "asm",
                "assembly_input_reads": ["reads.fq"],
                "assembly_threads": 4,
            },
        )
        sources = parameter_sources_payload(resolved)

        self.assertEqual(
            set(sources),
            {
                "l0_executor",
                "l1_facts",
                "l2_approved",
                "effective_parameters",
                "effective_source_by_parameter",
                "applications",
            },
        )
        self.assertEqual(sources["l0_executor"]["output_prefix"], "asm")
        self.assertEqual(sources["l1_facts"]["hg_size"], "1g")
        self.assertEqual(sources["l2_approved"], {"purge_level": 1})

    def test_resolver_materializes_hifiasm_thread_default(self):
        for method_id in (
            "assembly.candidate.hifiasm",
            "assembly.candidate.hifiasm_ont",
        ):
            with self.subTest(method_id=method_id):
                catalog = load_parameter_catalog(method_id)
                resolved = resolve_assembly_parameters(
                    catalog=catalog,
                    snapshot=_snapshot(method_id),
                    approved_l2={},
                    l0_executor_values={
                        "assembly_output_prefix": "/tmp/hifiasm",
                        "assembly_input_reads": ["/tmp/r.fq"],
                    },
                )
                argv = render_assembly_cli(catalog, resolved)

                self.assertEqual(resolved.l0_executor["threads"], 32)
                self.assertEqual(argv[argv.index("-t") + 1], "32")

    def test_hifiasm_genome_size_fact_transform_is_rendered(self):
        cases = [
            (500_000_000, "500m"),
            (3_100_000_000, "3.1g"),
            (2_000_000, "2m"),
            (12_345, "12.3k"),
            (500, "0.5k"),
        ]
        for genome_size, expected in cases:
            with self.subTest(genome_size=genome_size):
                resolved = resolve_assembly_parameters(
                    catalog=self.catalog,
                    snapshot=_snapshot(self.method_id, genome_size=genome_size),
                    approved_l2={},
                    l0_executor_values=_l0_executor_values(self.method_id),
                )
                argv = render_assembly_cli(self.catalog, resolved)

                self.assertEqual(resolved.l1_facts["hg_size"], expected)
                self.assertEqual(argv[argv.index("--hg-size") + 1], expected)

    def test_nextdenovo_config_renderer_uses_effective_values(self):
        method_id = "assembly.candidate.nextdenovo"
        catalog = load_parameter_catalog(method_id)
        resolved = resolve_assembly_parameters(
            catalog=catalog,
            snapshot=_snapshot(method_id),
            approved_l2={"read_cutoff": 7777, "nextgraph_a": 2},
            l0_executor_values=_l0_executor_values(method_id),
        )

        rendered = render_assembly_config(catalog, resolved)

        self.assertEqual(rendered["read_cutoff"], 7777)
        self.assertEqual(rendered["nextgraph_a"], 2)

    def test_quantity_facts_reject_legacy_string_and_boolean_values(self):
        for raw in ("500m", True, float("nan"), float("inf")):
            with self.subTest(raw=raw):
                with self.assertRaises(ValidationError):
                    QuantityFact.model_validate(
                        {
                            "value": raw,
                            "unit": "bp",
                            "origin": {
                                "kind": "provided",
                                "producer": "run_config",
                            },
                        }
                    )

    def test_all_catalog_source_paths_match_the_static_contract(self):
        lint_parameter_catalogs()

    def test_catalog_fact_key_typo_fails_closed(self):
        payload = {
            "catalog_id": "bad",
            "method_id": "assembly.candidate.bad",
            "tool": "bad",
            "parameters": {
                "genome_size": {
                    "value_type": "int",
                    "application": {
                        "kind": "cli_option",
                        "flag": "--genome-size",
                        "order": 1,
                    },
                    "layers": {"l1": {"fact": "genome_survey.estimates.genmoe_size"}},
                }
            },
        }

        with self.assertRaises(ValidationError):
            MethodParameterCatalog.model_validate(payload)


class ParameterRecommendationNodeTests(unittest.TestCase):
    def _approve(self, state: dict, recommendation_update: dict) -> dict:
        assembly = recommendation_update["stage_outputs"]["assembly"]
        interrupt = assembly["active_interrupt"]
        self.assertIsInstance(interrupt, dict, assembly)
        blocked = {
            **state,
            "current_stage": "assembly",
            "supervision_state": "blocked_human",
            "active_interrupt": interrupt,
            "stage_outputs": recommendation_update["stage_outputs"],
        }
        update = resume_review_state(
            blocked,
            ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly",
                gate_id="parameter_approval",
                review_request_id=interrupt["review_request_id"],
                review_request_hash=interrupt["request_hash"],
                decision="approve_parameters",
                metadata={
                    key: interrupt.get(key)
                    for key in (
                        "attempt_id",
                        "task",
                        "previous_attempt_id",
                        "recommendation_hash",
                    )
                },
            ),
        )
        return {**blocked, **update}

    def _state(self, tmpdir: str):
        state = _v2_state(tmpdir)
        paths = SessionPaths.from_run_dir(state["data_dir"])
        reads = paths.workflow_step_work_path(
            "step-000-inventory", "inventory", "fixture_reads", "reads.fastq"
        )
        reads.parent.mkdir(parents=True, exist_ok=True)
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        published_reads = publish_artifact_record(
            state["data_dir"],
            state["session_id"],
            ArtifactMeta.from_file(
                reads,
                stage="inventory",
                method_name="inventory.test_fixture",
                artifact_type="input.long_reads",
            ),
            destination=paths.workflow_step_artifact_path(
                "step-000-inventory", "inventory", "long_reads", "reads.fastq"
            ),
        )
        state["shared_parameters"] = {
            "organism": "Example species",
            "target_level": "draft",
        }
        read_qc_summary = {
            "n50": 30_000,
            "total_bases": 42_000_000_000,
        }
        genome_survey_result = {
            "estimates": {"genome_size": 1_000_000_000, "heterozygosity": 0.01},
            "estimate_sources": {"genome_size": "survey"},
        }
        _activate_step(state, 1)
        read_qc_ref = publish_parameter_json(
            state,
            stage="read_qc",
            scope="recommendation",
            identifier=0,
            filename="summary.json",
            payload={"schema_version": 2, **read_qc_summary},
            artifact_type="read_qc.summary",
            method_name="read_qc.test_fixture",
        )
        _activate_step(state, 2)
        genome_survey_ref = publish_parameter_json(
            state,
            stage="genome_survey",
            scope="recommendation",
            identifier=0,
            filename="result.json",
            payload={"schema_version": 2, **genome_survey_result},
            artifact_type="genome_survey.result",
            method_name="genome_survey.test_fixture",
        )
        _activate_step(state, 3)
        state["stage_outputs"] = {
            "read_qc": {
                "summary": read_qc_summary,
                "summary_artifact_ref": read_qc_ref.model_dump(mode="json"),
            },
            "genome_survey": {
                **genome_survey_result,
                "result_artifact_ref": genome_survey_ref.model_dump(mode="json"),
            },
            "assembly": {
                "status": "selected",
                "execution_mode": "local",
                "primary_method": "assembly.candidate.hifiasm_ont",
                "selected_route": "ont",
                "assembly_route": "ont",
                "target_level": "draft",
                "input_ref": str(published_reads.path),
                "input_type": "clean_long_reads",
                "assembly_input_member_ids": {"primary": ["fixture_member_0"]},
                "assembly_input_member_platforms": {"primary": ["ont"]},
                "assembly_input_artifact_refs": {
                    "primary": [published_reads.ref.model_dump(mode="json")]
                },
                "input_profile": {
                    "recommended_route": "ont",
                    "has_ont": True,
                    "has_hifi": False,
                },
                "assembly_selection": {
                    "read_type": "ont_raw",
                    "primary_method": "assembly.candidate.hifiasm_ont",
                    "source": "recommendation",
                    "rationale": "test",
                    "metadata": {},
                },
            },
        }
        return state

    def test_initial_recommendation_writes_replayable_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            payload = _complete_hifiasm_ont_response(
                purge_level=1, read_length_cutoff=3000
            )
            with patch(
                "app.llm.parameters.assembly.recommender.build_chat_model",
                return_value=_FakeModel(payload),
            ):
                result = recommend_assembly_parameters(state)
            artifact_records = read_artifact_records(Path(state["data_dir"]), "s1")
            assembly = result["stage_outputs"]["assembly"]
            cursor = ParameterRecommendationCursor.model_validate(
                assembly["current_parameter_recommendation"]
            )
            chain = load_parameter_recommendation_chain_v3(
                {**state, "stage_outputs": result["stage_outputs"]},
                manifest_ref=cursor.manifest_ref,
                expected_stage="assembly",
                expected_method_id="assembly.candidate.hifiasm_ont",
            )

        self.assertEqual(assembly["status"], "blocked_human")
        self.assertEqual(assembly.get("parameter_attempts") or [], [])
        recommendation = chain.recommendation["recommendation"]
        self.assertEqual(cursor.recommendation_id, 0)
        self.assertEqual(cursor.method_id, "assembly.candidate.hifiasm_ont")
        self.assertEqual(
            chain.manifest.recommendation_contract,
            "parameter_recommendation_v1",
        )
        self.assertEqual(
            recommendation["parameters"],
            {"purge_level": 1, "read_length_cutoff": 3000},
        )
        self.assertEqual(
            chain.catalog.method_id,
            "assembly.candidate.hifiasm_ont",
        )
        self.assertTrue(chain.response_parse["parsed"])
        refs = chain.manifest.refs
        self.assertIn(
            "parameter_recommendations/000/input_snapshot.json",
            refs.input_snapshot_ref.path_ref,
        )
        self.assertIn(
            "parameter_recommendations/000/model_request.json",
            refs.model_request_ref.path_ref,
        )
        self.assertIn(
            "parameter_recommendations/000/model_response.json",
            refs.model_response_ref.path_ref,
        )
        self.assertIn(
            "parameter_recommendations/000/response_parse.json",
            refs.response_parse_ref.path_ref,
        )
        artifact_types = {record["artifact_type"] for record in artifact_records}
        self.assertIn(
            "assembly.parameter_recommendation.input_snapshot", artifact_types
        )
        self.assertIn("assembly.parameter.model_request", artifact_types)
        self.assertIn("assembly.parameter.recommendation_manifest", artifact_types)

    def test_model_unavailable_fails_before_execution_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            with patch(
                "app.llm.parameters.assembly.recommender.build_chat_model",
                side_effect=RuntimeError("missing api key"),
            ):
                result = recommend_assembly_parameters(state)
            assembly = result["stage_outputs"]["assembly"]
            cursor = ParameterRecommendationCursor.model_validate(
                assembly["current_parameter_recommendation"]
            )
            chain = load_parameter_recommendation_chain_v3(
                {**state, "stage_outputs": result["stage_outputs"]},
                manifest_ref=cursor.manifest_ref,
                expected_stage="assembly",
                expected_method_id="assembly.candidate.hifiasm_ont",
            )

        self.assertEqual(assembly["status"], "parameter_response_invalid")
        self.assertEqual(cursor.status, "response_invalid")
        self.assertIn(
            "missing api key",
            chain.response_parse["error"],
        )
        self.assertEqual(assembly.get("parameter_attempts") or [], [])

    def test_any_parsed_parameter_mapping_reaches_human_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            payload = _parameter_response(
                threads=64, purge_level=9, unknown_knob="review-me"
            )
            with patch(
                "app.llm.parameters.assembly.recommender.build_chat_model",
                return_value=_FakeModel(payload),
            ):
                result = recommend_assembly_parameters(state)
            assembly = result["stage_outputs"]["assembly"]
            cursor = ParameterRecommendationCursor.model_validate(
                assembly["current_parameter_recommendation"]
            )
            chain = load_parameter_recommendation_chain_v3(
                {**state, "stage_outputs": result["stage_outputs"]},
                manifest_ref=cursor.manifest_ref,
                expected_stage="assembly",
                expected_method_id="assembly.candidate.hifiasm_ont",
            )

        self.assertEqual(assembly["status"], "blocked_human")
        self.assertEqual(
            chain.recommendation["recommendation"]["parameters"],
            {"threads": 64, "purge_level": 9, "unknown_knob": "review-me"},
        )

    def test_execution_retry_gate_keeps_stderr_in_execution_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stderr_text = "\n".join(f"line {idx}" for idx in range(150))
            state = _v2_state(tmpdir)
            attempt = _persist_parameter_attempt(
                state,
                method_id="assembly.candidate.hifiasm_ont",
                status="execution_failed",
                execution_result={"return_code": 1},
                execution_stderr=stderr_text,
            )
            state["stage_outputs"] = {
                "assembly": {
                    "status": "failed",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                }
            }

            result = execution_retry_gate(state)
            assembly = result["stage_outputs"]["assembly"]
            execution = load_execution_evidence(
                state,
                assembly["parameter_retry_request"]["failure_ref"],
                expected_stage="assembly",
                expected_method_name="assembly.candidate.hifiasm_ont",
                expected_frozen_attempt_ref=attempt["frozen_attempt_ref"],
            )

            self.assertEqual(
                assembly["status"], "retry_parameters_after_execution_failure"
            )
            self.assertEqual(assembly["current_parameter_attempt_id"], 0)
            self.assertEqual(assembly["proposed_parameter_attempt_id"], 1)
            self.assertNotIn("execution_failure_log_tail", assembly)
            self.assertNotIn("stderr_path", execution.model_dump(mode="json"))
            stderr_path = resolve_artifact_ref(
                state["data_dir"],
                state["session_id"],
                execution.stderr_ref,
            )
            self.assertIn("line 149", stderr_path.read_text(encoding="utf-8"))
            self.assertNotIn("failure_class", assembly["parameter_attempts"][0])

    def test_execution_retry_prompt_includes_bounded_stderr_tail_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            stderr_text = "discarded diagnostic\n" * 2_000 + "tail diagnostic marker\n"
            attempt = _persist_parameter_attempt(
                state,
                method_id="assembly.candidate.hifiasm_ont",
                recommended_params={"purge_level": 1},
                status="execution_failed",
                execution_result={"return_code": 17, "status": "failed"},
                execution_stderr=stderr_text,
            )
            state["stage_outputs"]["assembly"].update(
                {
                    "status": "failed",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                }
            )
            retry_gate = execution_retry_gate(state)
            state["stage_outputs"] = retry_gate["stage_outputs"]
            retry_model = _FakeModel(_complete_hifiasm_ont_response(purge_level=2))

            with patch(
                "app.llm.parameters.assembly.recommender.build_chat_model",
                return_value=retry_model,
            ):
                recommendation = recommend_assembly_parameters(state)

            prompt_request = json.loads(retry_model.messages[1]["content"])
            serialized = json.dumps(prompt_request, ensure_ascii=False)

        stderr_tail = prompt_request["assembly_context"]["retry"]["attempt_history"][
            -1
        ]["execution"]["stderr_tail"]
        self.assertTrue(stderr_tail.endswith("tail diagnostic marker"))
        self.assertLessEqual(len(stderr_tail), 4_000)
        self.assertEqual(
            recommendation["stage_outputs"]["assembly"]["status"], "blocked_human"
        )
        self.assertNotIn("stderr_ref", serialized)
        self.assertNotIn("path_ref", serialized)
        self.assertNotIn("parameter_attempt_0.stderr.log", serialized)

    def test_execution_retry_gate_does_not_apply_hifiasm_classifier_to_other_methods(
        self,
    ):
        for method_id in (
            "assembly.candidate.flye",
            "assembly.candidate.nextdenovo",
            "assembly.candidate.hifiasm_ont",
        ):
            with (
                self.subTest(method_id=method_id),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                state = _v2_state(tmpdir)
                attempt = _persist_parameter_attempt(
                    state,
                    method_id=method_id,
                    status="execution_failed",
                    execution_result={"return_code": 2},
                    execution_stderr="invalid parameter purge_level",
                )
                state["stage_outputs"] = {
                    "assembly": {
                        "status": "failed",
                        "current_parameter_attempt_id": 0,
                        "parameter_attempts": [attempt],
                    }
                }
                result = execution_retry_gate(state)
                attempt = result["stage_outputs"]["assembly"]["parameter_attempts"][0]
                self.assertNotIn("failure_class", attempt)
                self.assertNotIn("candidate_evidence_case_ref", attempt)
                self.assertIsNotNone(
                    result["stage_outputs"]["assembly"]["parameter_retry_request"][
                        "failure_ref"
                    ]
                )

    def test_execution_retry_gate_emits_no_candidate_and_binds_sealed_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _v2_state(tmpdir)
            attempt = _persist_parameter_attempt(
                state,
                method_id="assembly.candidate.hifiasm",
                status="execution_failed",
                execution_result={
                    "success": False,
                    "return_code": 2,
                    "status": "failed",
                    "status_detail": "",
                },
                execution_stderr="invalid parameter purge_level",
            )
            state["stage_outputs"] = {
                "assembly": {
                    "status": "failed",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                }
            }
            result = execution_retry_gate(state)
            attempt = result["stage_outputs"]["assembly"]["parameter_attempts"][0]
            self.assertNotIn("candidate_evidence_case_ref", attempt)
            request = result["stage_outputs"]["assembly"]["parameter_retry_request"]
            self.assertEqual(request["previous_attempt_id"], 0)
            self.assertEqual(
                request["previous_attempt_seal_hash"], attempt["seal_hash"]
            )
            self.assertEqual(request["failure_ref"], attempt["execution_ref"])

    def test_freeze_failure_without_execution_result_does_not_route_to_execution_retry(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _v2_state(tmpdir)
            attempt = _persist_parameter_attempt(
                state,
                method_id="assembly.candidate.flye",
            )
            state["stage_outputs"] = {
                "assembly": {
                    "status": "failed",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                }
            }

            self.assertEqual(route_after_method_subgraph(state), "fail")
            self.assertEqual(execution_retry_gate(state), {})

    def test_review_quality_diagnosis_preserves_frozen_assembly_occurrence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _v2_state(tmpdir)
            attempt = _persist_parameter_attempt(
                state,
                method_id="assembly.candidate.hifiasm_ont",
                status="executed",
            )
            state["stage_outputs"] = {
                "assembly": {
                    "status": "draft_ready",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                },
                "assembly_review": {
                    "metrics": {
                        "busco_complete": 0.94,
                        "busco_duplicate": 0.21,
                        "assembly_size_ratio": 1.35,
                    }
                },
            }
            _activate_step(state, 4)
            before_assembly = json.loads(json.dumps(state["stage_outputs"]["assembly"]))
            before_index = state["workflow_step_index"]
            before_entered_index = state["entered_workflow_step_index"]

            result = {**state, **assembly_review_parameter_quality_gate(state)}

            assembly = result["stage_outputs"]["assembly"]
            review = result["stage_outputs"]["assembly_review"]
            self.assertEqual(assembly, before_assembly)
            self.assertEqual(result["current_stage"], "assembly_review")
            self.assertEqual(result["workflow_step_index"], before_index)
            self.assertEqual(
                result["entered_workflow_step_index"], before_entered_index
            )
            self.assertEqual(
                review["parameter_quality_gate"]["failure_mode"], "under_purged"
            )
            self.assertTrue(review["parameter_quality_checked"])
            self.assertIsNone(assembly["parameter_attempts"][0]["review_ref"])
            self.assertEqual(
                route_after_parameter_quality_gate(result),
                "related_species_sanity_check",
            )
            self.assertNotIn("replan", build_assembly_review_stage().get_graph().nodes)

    def test_review_quality_diagnosis_rejects_non_finite_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _v2_state(tmpdir)
            attempt = _persist_parameter_attempt(
                state,
                method_id="assembly.candidate.hifiasm_ont",
                status="executed",
            )
            state["stage_outputs"] = {
                "assembly": {
                    "status": "draft_ready",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                },
                "assembly_review": {
                    "metrics": {
                        "busco_complete": math.nan,
                        "busco_duplicate": math.inf,
                    }
                },
            }
            _activate_step(state, 4)

            with self.assertRaisesRegex(ValueError, "non-finite float"):
                assembly_review_parameter_quality_gate(state)

    def test_assembly_review_graph_contains_parameter_quality_gate(self):
        graph = build_assembly_review_stage().get_graph()

        self.assertIn("parameter_quality_gate", set(graph.nodes))


class MethodFreezeParameterFailureTests(unittest.TestCase):
    def _publish_reads(self, state: dict, reads: Path) -> tuple[str, dict]:
        paths = SessionPaths.from_run_dir(state["data_dir"])
        published = publish_artifact_record(
            state["data_dir"],
            state["session_id"],
            ArtifactMeta.from_file(
                reads,
                stage="inventory",
                method_name="inventory.test_fixture",
                artifact_type="input.long_reads",
            ),
            destination=paths.workflow_step_artifact_path(
                "step-000-inventory", "inventory", "long_reads", reads.name
            ),
        )
        return str(published.path), published.ref.model_dump(mode="json")

    def _attach_planned_candidate(
        self,
        state: dict,
        assembly: dict,
        *,
        method_name: str,
    ) -> None:
        paths = SessionPaths.from_run_dir(state["data_dir"])
        short_name = method_name.rsplit(".", 1)[-1]
        source = paths.workflow_step_work_path(
            "step-003-assembly",
            "assembly",
            "fixture_candidate_planning",
            f"{short_name}.planned.json",
        )
        source.parent.mkdir(parents=True, exist_ok=True)
        bundle = AssemblyCandidateBundle(
            session_id=state["session_id"],
            candidate_id=f"{state['session_id']}:{method_name}:parameter_attempt_0",
            method_name=method_name,
            status="planned",
            input_type=assembly["input_type"],
            input_member_ids=dict(assembly["assembly_input_member_ids"]),
            input_member_platforms=dict(
                assembly["assembly_input_member_platforms"]
            ),
            parameter_attempt_id=0,
            expected_outputs=["primary_assembly"],
        )
        source.write_text(
            json.dumps(bundle.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        artifact = ArtifactMeta.from_file(
            source,
            stage="assembly",
            method_name=method_name,
            artifact_type="draft_assembly.candidate",
        )
        published = publish_artifact_record(
            state["data_dir"],
            state["session_id"],
            artifact,
            destination=paths.workflow_step_artifact_path(
                "step-003-assembly",
                "assembly",
                "attempts",
                "000",
                "candidates",
                f"{short_name}.planned.json",
            ),
        )
        assembly["planned_candidate_bundle_ref"] = published.ref.model_dump(
            mode="json"
        )

    def _parameter_attempt(
        self, state: dict, *, method_name: str, recommended_params=None
    ) -> dict:
        return _persist_parameter_attempt(
            state,
            method_id=method_name,
            recommended_params=dict(recommended_params or {}),
        )

    def _state(
        self,
        tmpdir: str,
        *,
        method: str,
        recommended_params=None,
        attempt_payload: dict | None = None,
    ):
        state = _v2_state(tmpdir)
        reads = SessionPaths.from_run_dir(state["data_dir"]).workflow_step_work_path(
            "step-000-inventory", "inventory", "fixture_reads", "reads.fastq"
        )
        reads.parent.mkdir(parents=True, exist_ok=True)
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        reads_ref, reads_artifact_ref = self._publish_reads(state, reads)
        if method == "hifiasm":
            status = "hifiasm_execution_planned"
            param_key = "hifiasm_parameters"
            method_name = "assembly.candidate.hifiasm"
        else:
            status = "hifiasm_ont_execution_planned"
            param_key = "hifiasm_ont_parameters"
            method_name = "assembly.candidate.hifiasm_ont"
        assembly = {
            "status": status,
            "input_ref": reads_ref,
            "input_type": "clean_long_reads",
            "env": {},
            "assembly_selection": {
                "read_type": "pacbio_hifi" if method == "hifiasm" else "ont_raw",
                "primary_method": method_name,
                "source": "run_config",
                "rationale": "test fixture explicitly selects the assembly method",
            },
            param_key: {"threads": 1},
            "current_parameter_attempt_id": 0,
            "assembly_input_member_ids": {"primary": ["fixture_member_0"]},
            "assembly_input_member_platforms": {
                "primary": ["pacbio_hifi" if method == "hifiasm" else "ont"]
            },
            "assembly_input_artifact_refs": {
                "primary": [reads_artifact_ref]
            },
        }
        if attempt_payload is not None:
            assembly["parameter_attempts"] = [attempt_payload]
        elif recommended_params is not None:
            assembly["parameter_attempts"] = [
                self._parameter_attempt(
                    state,
                    method_name=method_name,
                    recommended_params=recommended_params,
                )
            ]
        assembly[ASSEMBLY_EXECUTION_CONTRACT_KEY] = build_assembly_execution_contract(
            assembly,
            method_id=method_name,
        ).model_dump(mode="json")
        self._attach_planned_candidate(
            state,
            assembly,
            method_name=method_name,
        )
        state["stage_outputs"] = {"assembly": assembly}
        return state

    def _nextdenovo_state(
        self,
        tmpdir: str,
        *,
        recommended_params=None,
        attempt_payload: dict | None = None,
    ):
        state = _v2_state(tmpdir)
        reads = SessionPaths.from_run_dir(state["data_dir"]).workflow_step_work_path(
            "step-000-inventory", "inventory", "fixture_reads", "reads.fastq"
        )
        reads.parent.mkdir(parents=True, exist_ok=True)
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        reads_ref, reads_artifact_ref = self._publish_reads(state, reads)
        assembly = {
            "status": "nextdenovo_execution_planned",
            "input_ref": reads_ref,
            "input_type": "clean_long_reads",
            "env": {},
            "assembly_selection": {
                "read_type": "ont_raw",
                "primary_method": "assembly.candidate.nextdenovo",
                "source": "run_config",
                "rationale": "test fixture explicitly selects the assembly method",
            },
            "nextdenovo_parameters": {
                "genome_size": 2_000_000,
                "threads": 1,
            },
            "assembly_input_member_ids": {"primary": ["fixture_member_0"]},
            "assembly_input_member_platforms": {"primary": ["ont"]},
            "assembly_input_artifact_refs": {
                "primary": [reads_artifact_ref]
            },
            "resolved_genome_size": {
                "value_bp": 2_000_000,
                "source": "provided",
                "source_path": "assembly.nextdenovo_parameters.genome_size",
            },
            "current_parameter_attempt_id": 0,
        }
        if attempt_payload is not None:
            assembly["parameter_attempts"] = [attempt_payload]
        elif recommended_params is not None:
            assembly["parameter_attempts"] = [
                self._parameter_attempt(
                    state,
                    method_name="assembly.candidate.nextdenovo",
                    recommended_params=recommended_params,
                )
            ]
        assembly[ASSEMBLY_EXECUTION_CONTRACT_KEY] = build_assembly_execution_contract(
            assembly,
            method_id="assembly.candidate.nextdenovo",
        ).model_dump(mode="json")
        self._attach_planned_candidate(
            state,
            assembly,
            method_name="assembly.candidate.nextdenovo",
        )
        state["stage_outputs"] = {"assembly": assembly}
        return state

    def test_invalid_hifiasm_approved_bundle_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm")
            with (
                patch(
                    "app.stages.assembly.method_application.hifiasm.resolve_method_env",
                    return_value=(None, [], None),
                ),
                patch(
                    "app.stages.assembly.method_application.hifiasm.load_current_approved_recommendation",
                    return_value=(
                        None,
                        "Hifiasm recommendation evidence is invalid: "
                        "missing catalog snapshot",
                    ),
                ),
            ):
                result = freeze_hifiasm_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("recommendation evidence is invalid", assembly["reason"])

    def test_hifiasm_freeze_rejects_loose_attempt_cursor_before_work_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                method="hifiasm",
                recommended_params={"purge_level": 1},
            )
            state["stage_outputs"]["assembly"]["current_parameter_attempt_id"] = "0"
            work_dir = SessionPaths.from_run_dir(
                state["data_dir"]
            ).workflow_step_work_path(
                "step-003-assembly", "assembly", "attempts", "000", "hifiasm"
            )

            with patch(
                "app.stages.assembly.method_application.hifiasm.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_hifiasm_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("explicit non-negative integer", assembly["reason"])
        self.assertFalse(work_dir.exists())

    def test_corrupt_hifiasm_recommended_params_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm", recommended_params={})
            payload = state["stage_outputs"]["assembly"]["parameter_attempts"][0]
            payload["recommended_params"] = ["param1", "param2"]
            with patch(
                "app.stages.assembly.method_application.hifiasm.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_hifiasm_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("ParameterAttempt", assembly["reason"])
        self.assertIn("recommended_params", assembly["reason"])

    def test_invalid_hifiasm_ont_approved_bundle_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm_ont")
            with (
                patch(
                    "app.stages.assembly.method_application.hifiasm_ont.resolve_method_env",
                    return_value=(None, [], None),
                ),
                patch(
                    "app.stages.assembly.method_application.hifiasm_ont.load_current_approved_recommendation",
                    return_value=(
                        None,
                        "Hifiasm ONT recommendation evidence is invalid: "
                        "missing catalog snapshot",
                    ),
                ),
            ):
                result = freeze_hifiasm_ont_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("recommendation evidence is invalid", assembly["reason"])

    def test_corrupt_hifiasm_ont_recommended_params_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm_ont", recommended_params={})
            payload = state["stage_outputs"]["assembly"]["parameter_attempts"][0]
            payload["recommended_params"] = "invalid"
            with patch(
                "app.stages.assembly.method_application.hifiasm_ont.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_hifiasm_ont_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("ParameterAttempt", assembly["reason"])
        self.assertIn("recommended_params", assembly["reason"])

    def test_hifiasm_ont_freeze_rejects_untyped_parameter_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                method="hifiasm_ont",
                attempt_payload={
                    "attempt_id": 0,
                    "recommended_params": {"purge_level": 1},
                },
            )
            with patch(
                "app.stages.assembly.method_application.hifiasm_ont.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_hifiasm_ont_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("ParameterAttempt", assembly["reason"])
        self.assertIn("task", assembly["reason"])

    def test_hifiasm_ont_freeze_ignores_corrupt_non_current_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                method="hifiasm_ont",
                recommended_params={"purge_level": 1},
            )
            state["stage_outputs"]["assembly"]["parameter_attempts"].append(
                {"attempt_id": 1}
            )
            with patch(
                "app.stages.assembly.method_application.hifiasm_ont.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_hifiasm_ont_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "hifiasm_ont_frozen")
        self.assertEqual(assembly["parameter_attempts"][0]["status"], "frozen")
        self.assertEqual(assembly["parameter_attempts"][1], {"attempt_id": 1})

    def test_hifiasm_ont_freeze_writes_command_artifact_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir, method="hifiasm_ont", recommended_params={"purge_level": 1}
            )
            with patch(
                "app.stages.assembly.method_application.hifiasm_ont.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_hifiasm_ont_attempt(state)
            artifact_records = read_artifact_records(Path(state["data_dir"]), "s1")

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "hifiasm_ont_frozen")
            command_ref = assembly["parameter_attempts"][0]["command_ref"]
            self.assertIn("attempts/000/command.json", command_ref["path_ref"])
            command_path = _ref_path(state, command_ref)
            self.assertTrue(command_path.exists())
            self.assertEqual(command_ref["sha256"], sha256_file(command_path))
            command_payload = json.loads(command_path.read_text(encoding="utf-8"))
            self.assertEqual(
                command_payload["method_id"], "assembly.candidate.hifiasm_ont"
            )
            self.assertIn("--ont", command_payload["argv"])
            self.assertIn(
                "assembly.parameter.command",
                {record["artifact_type"] for record in artifact_records},
            )

    def test_nextdenovo_freeze_rejects_l2_override_of_l0_parameter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._nextdenovo_state(tmpdir, recommended_params={"threads": 64})
            with patch(
                "app.stages.assembly.method_application.nextdenovo.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_nextdenovo_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("NextDenovo parameter sources are invalid", assembly["reason"])
        self.assertIn("not cataloged as L2-adjustable", assembly["reason"])

    def test_nextdenovo_freeze_renders_l2_read_cutoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._nextdenovo_state(
                tmpdir, recommended_params={"read_cutoff": 7777, "nextgraph_a": 2}
            )
            with patch(
                "app.stages.assembly.method_application.nextdenovo.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_nextdenovo_attempt(state)
            run_cfg_text = Path(
                result["stage_outputs"]["assembly"]["nextdenovo_run_cfg_path"]
            ).read_text(encoding="utf-8")
            command = read_json_artifact(
                state,
                result["stage_outputs"]["assembly"]["parameter_attempts"][0][
                    "command_ref"
                ],
            )

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "nextdenovo_frozen")
        self.assertEqual(
            command["parameter_sources"]["l2_approved"]["read_cutoff"], 7777
        )
        self.assertEqual(command["rendered_parameters"]["read_cutoff"], 7777)
        self.assertEqual(command["rendered_parameters"]["nextgraph_a"], 2)
        self.assertIn("read_cutoff = 7777", run_cfg_text)
        self.assertIn("nextgraph_options = -a 2", run_cfg_text)

    def test_nextdenovo_freeze_rejects_corrupt_l2_integer_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._nextdenovo_state(
                tmpdir, recommended_params={"read_cutoff": "bad"}
            )
            with patch(
                "app.stages.assembly.method_application.nextdenovo.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_nextdenovo_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("NextDenovo parameter sources are invalid", assembly["reason"])
        self.assertIn("read_cutoff cannot be rendered as int", assembly["reason"])


if __name__ == "__main__":
    unittest.main()
