"""Focused Assembly parameter-resolution and rendering tests for schema v3."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.domains.assembly.constants import (
    FLYE_METHOD,
    HIFIASM_METHOD,
    HIFIASM_ONT_METHOD,
    NEXTDENOVO_METHOD,
)
from app.domains.assembly.method_guidance import load_method_guidance
from app.domains.assembly.selection import AssemblySelection
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalGenomeFacts,
    CanonicalRecommendationFacts,
    ExecutionFailureTrigger,
    ParameterAttemptEvidenceV3,
    QuantityFact,
    QuantityOrigin,
    RecommendationInputSnapshotV3,
    RecommendationRetryContext,
)
from app.schemas.state import initial_state
from app.stages.assembly.command_builder import (
    ParameterCommandError,
    parameter_sources_payload,
    render_assembly_cli,
    render_assembly_config,
    resolve_assembly_parameters,
)
from app.stages.assembly.method_application import flye as flye_adapter
from app.stages.assembly.method_application import hifiasm as hifiasm_adapter
from app.stages.assembly.method_application import hifiasm_ont as hifiasm_ont_adapter
from app.stages.assembly.method_application import nextdenovo as nextdenovo_adapter
from app.stages.assembly.method_application import support as assembly_support
from app.stages.assembly.method_application.nextdenovo import _run_cfg
from app.stages.assembly.parameters.catalog import (
    lint_parameter_catalogs,
    load_parameter_catalog,
)
from app.stages.assembly.parameters.context import (
    build_assembly_recommendation_context,
)
from app.storage.parameter_evidence import ParameterEvidenceError
from app.storage.paths import SessionPaths, ensure_run_layout


def _assembly_state(run_dir: Path, output: dict) -> dict:
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
        workflow_steps=[
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "step_id": "step-001-assembly",
                "stage": "assembly",
                "allow_local_execution": False,
            },
            {
                "step_id": "step-002-assembly_review",
                "stage": "assembly_review",
                "allow_local_execution": False,
                "review_target": {
                    "resource_type": "assembly",
                    "producer_step_id": "step-001-assembly",
                },
            },
        ],
    )
    state.update(
        {
            "current_stage": "assembly",
            "workflow_step_index": 1,
            "entered_workflow_step_index": 1,
            "stage_outputs": {"assembly": output},
        }
    )
    return state


def _snapshot(
    method_id: str,
    *,
    genome_size: int = 2_500_000,
) -> RecommendationInputSnapshotV3:
    if method_id == HIFIASM_METHOD:
        route = "hifi"
        read_type = "pacbio_hifi"
    elif method_id == FLYE_METHOD:
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


class AssemblyCatalogV3Tests(unittest.TestCase):
    def test_catalogs_use_short_catalog_ids_and_stable_method_ids(self):
        expected = {
            FLYE_METHOD: "flye",
            NEXTDENOVO_METHOD: "nextdenovo",
            HIFIASM_METHOD: "hifiasm",
            HIFIASM_ONT_METHOD: "hifiasm_ont",
        }

        lint_parameter_catalogs()
        for method_id, catalog_id in expected.items():
            with self.subTest(method_id=method_id):
                catalog = load_parameter_catalog(method_id)
                self.assertEqual(catalog.catalog_id, catalog_id)
                self.assertEqual(catalog.method_id, method_id)
                with self.assertRaisesRegex(ValueError, "Unknown method descriptor"):
                    load_parameter_catalog(catalog_id)

    def test_guidance_uses_only_stable_assembly_method_ids(self):
        registry = load_method_guidance()
        stable_ids = {
            FLYE_METHOD,
            NEXTDENOVO_METHOD,
            HIFIASM_METHOD,
            HIFIASM_ONT_METHOD,
        }

        self.assertTrue(registry.records)
        for record in registry.records:
            self.assertTrue(set(record.methods).issubset(stable_ids))


class AssemblyContextV3Tests(unittest.TestCase):
    def test_context_uses_confirmed_selected_route_when_run_config_is_auto(self):
        selection = AssemblySelection(
            read_type="pacbio_hifi",
            primary_method=HIFIASM_METHOD,
            source="human",
            rationale="confirmed",
        ).model_dump(mode="json")
        context = build_assembly_recommendation_context(
            {},
            assembly_out={
                "assembly_selection": selection,
                "assembly_route": "auto",
                "selected_route": "hifi",
                "target_level": "draft",
            },
        )
        self.assertEqual(context.assembly_route, "hifi")

    def test_context_uses_selection_and_explicit_stage_fields_only(self):
        retry = RecommendationRetryContext(
            trigger=ExecutionFailureTrigger(status="execution_failed"),
            attempt_history=(
                ParameterAttemptEvidenceV3(
                    attempt_id=0,
                    task="recommend_initial",
                    status="execution_failed",
                    recommended_parameters={},
                    approved_parameters={},
                    effective_parameters={},
                    effective_sources={},
                ),
            ),
        )
        selection = AssemblySelection(
            read_type="ont_hq",
            primary_method=FLYE_METHOD,
            source="run_config",
            rationale="test",
        )
        assembly_out = {
            "assembly_selection": selection.model_dump(mode="json"),
            "assembly_route": "ont",
            "target_level": "near_t2t",
            "input_profile": {
                "recommended_route": "hifi",
                "read_type": "hifi",
                "target_level": "draft",
            },
        }

        context = build_assembly_recommendation_context(
            {"shared_parameters": {"target_level": "strict_t2t"}},
            assembly_out=assembly_out,
            retry_context=retry,
        )

        self.assertEqual(context.assembly_route, "ont")
        self.assertEqual(context.read_type, "ont_hq")
        self.assertEqual(context.target_level, "near_t2t")
        self.assertEqual(context.retry, retry)

    def test_context_preserves_registered_selection_read_types(self):
        cases = (
            (FLYE_METHOD, "cyclone", "cyclone"),
            (FLYE_METHOD, "pacbio_clr", "cyclone"),
            (NEXTDENOVO_METHOD, "ont_raw", "ont"),
            (HIFIASM_METHOD, "pacbio_hifi", "hifi"),
            (HIFIASM_ONT_METHOD, "ont_hq", "ont"),
        )

        for method_id, selection_read_type, route in cases:
            with self.subTest(method_id=method_id, read_type=selection_read_type):
                selection = AssemblySelection(
                    read_type=selection_read_type,
                    primary_method=method_id,
                    source="run_config",
                    rationale="test",
                )
                context = build_assembly_recommendation_context(
                    {},
                    assembly_out={
                        "assembly_selection": selection.model_dump(mode="json"),
                        "assembly_route": route,
                        "target_level": "draft",
                    },
                )

                self.assertEqual(context.read_type, selection_read_type)

    def test_context_fails_when_explicit_inputs_are_missing_or_unknown(self):
        selection = AssemblySelection(
            read_type="pacbio_hifi",
            primary_method=HIFIASM_METHOD,
            source="run_config",
            rationale="test",
        ).model_dump(mode="json")
        base = {
            "assembly_selection": selection,
            "assembly_route": "hifi",
            "target_level": "draft",
            "input_profile": {
                "recommended_route": "hifi",
                "target_level": "draft",
            },
        }
        cases = (
            ("assembly_selection", "requires assembly_selection", None),
            ("assembly_route", "requires explicit assembly_route", None),
            ("target_level", "requires explicit target_level", None),
            ("assembly_route", "unknown assembly_route", "auto"),
            ("target_level", "unknown target_level", "chromosome"),
        )

        for key, message, replacement in cases:
            with self.subTest(key=key, replacement=replacement):
                assembly_out = dict(base)
                if replacement is None:
                    assembly_out.pop(key)
                else:
                    assembly_out[key] = replacement
                with self.assertRaisesRegex(ValueError, message):
                    build_assembly_recommendation_context(
                        {},
                        assembly_out=assembly_out,
                    )


class AssemblyRenderingV3Tests(unittest.TestCase):
    def test_flye_renders_read_flag_adjacent_reads_and_genome_size(self):
        catalog = load_parameter_catalog(FLYE_METHOD)
        resolved = resolve_assembly_parameters(
            catalog=catalog,
            snapshot=_snapshot(FLYE_METHOD),
            approved_l2={"asm_coverage": 40},
            l0_executor_values={
                "assembly_method_read_type": "nano-hq",
                "assembly_input_reads": ["/reads/a.fastq.gz", "/reads/b.fastq.gz"],
                "assembly_output_dir": "/work/flye",
                "assembly_threads": 8,
            },
        )

        self.assertEqual(
            render_assembly_cli(catalog, resolved),
            [
                "flye",
                "--nano-hq",
                "/reads/a.fastq.gz",
                "/reads/b.fastq.gz",
                "--out-dir",
                "/work/flye",
                "--genome-size",
                "2500000",
                "--threads",
                "8",
                "--asm-coverage",
                "40",
            ],
        )

    def test_nextdenovo_config_preserves_sparse_optional_l2_values(self):
        catalog = load_parameter_catalog(NEXTDENOVO_METHOD)
        l0 = {
            "assembly_method_read_type": "ont",
            "assembly_input_reads": "/work/input.fofn",
            "assembly_output_dir": "/work/nextdenovo_work",
            "assembly_threads": 12,
        }
        cases = (({}, False), ({"read_cutoff": 1000}, True))

        for approved_l2, has_cutoff in cases:
            with self.subTest(approved_l2=approved_l2):
                resolved = resolve_assembly_parameters(
                    catalog=catalog,
                    snapshot=_snapshot(NEXTDENOVO_METHOD),
                    approved_l2=approved_l2,
                    l0_executor_values=l0,
                )
                rendered = render_assembly_config(catalog, resolved)
                text = _run_cfg(**rendered)

                self.assertEqual(rendered["workdir"], "/work/nextdenovo_work")
                self.assertIn("workdir = /work/nextdenovo_work", text)
                self.assertEqual("read_cutoff = 1000" in text, has_cutoff)
                self.assertIn("nextgraph_options = -a 1\n", text)
                self.assertEqual(
                    resolved.effective_source_by_parameter["nextgraph_a"], "l0"
                )

        for value in (0, 1, 2, 3, 4, 5, 10):
            with self.subTest(nextgraph_a=value):
                with self.assertRaisesRegex(
                    ParameterCommandError, "not cataloged as L2-adjustable"
                ):
                    resolve_assembly_parameters(
                        catalog=catalog,
                        snapshot=_snapshot(NEXTDENOVO_METHOD),
                        approved_l2={"nextgraph_a": value},
                        l0_executor_values=l0,
                    )
                if value != 1:
                    with self.assertRaisesRegex(ValueError, "FASTA"):
                        _run_cfg(
                            read_type="ont",
                            input_fofn="/work/input.fofn",
                            workdir="/work/nextdenovo_work",
                            genome_size=2500000,
                            threads=12,
                            nextgraph_a=value,
                        )

    def test_hifiasm_commands_do_not_fabricate_route_inputs(self):
        forbidden = {
            "--primary",
            "-1",
            "-2",
            "-3",
            "-4",
            "--h1",
            "--h2",
            "--enzyme",
            "--ul",
            "--sec-in",
        }

        for method_id in (HIFIASM_METHOD, HIFIASM_ONT_METHOD):
            with self.subTest(method_id=method_id):
                catalog = load_parameter_catalog(method_id)
                resolved = resolve_assembly_parameters(
                    catalog=catalog,
                    snapshot=_snapshot(method_id),
                    approved_l2={"kmer_length": 31},
                    l0_executor_values={
                        "assembly_output_prefix": "/work/hifiasm/out",
                        "assembly_input_reads": ["/reads/long.fastq.gz"],
                        "assembly_threads": 16,
                    },
                )
                argv = render_assembly_cli(catalog, resolved)

                self.assertTrue(forbidden.isdisjoint(argv))
                self.assertEqual(argv[-1], "/reads/long.fastq.gz")
                self.assertEqual("--ont" in argv, method_id == HIFIASM_ONT_METHOD)

    def test_hifiasm_renders_multifile_supplementary_read_roles(self):
        catalog = load_parameter_catalog(HIFIASM_METHOD)
        resolved = resolve_assembly_parameters(
            catalog=catalog,
            snapshot=_snapshot(HIFIASM_METHOD),
            approved_l2={"kmer_length": 31},
            l0_executor_values={
                "assembly_output_prefix": "/work/hifiasm/out",
                "assembly_input_reads": [
                    "/reads/hifi-a.fastq.gz",
                    "/reads/hifi-b.fastq.gz",
                ],
                "assembly_ultralong_reads": [
                    "/reads/ont-a.fastq.gz",
                    "/reads/ont-b.fastq.gz",
                ],
                "assembly_hic_r1_reads": [
                    "/reads/hic-L1-R1.fastq.gz",
                    "/reads/hic-L2-R1.fastq.gz",
                ],
                "assembly_hic_r2_reads": [
                    "/reads/hic-L1-R2.fastq.gz",
                    "/reads/hic-L2-R2.fastq.gz",
                ],
                "assembly_threads": 16,
            },
        )

        argv = render_assembly_cli(catalog, resolved)

        self.assertNotIn("--ont", argv)
        self.assertEqual(
            argv[argv.index("--ul") + 1],
            "/reads/ont-a.fastq.gz,/reads/ont-b.fastq.gz",
        )
        self.assertEqual(
            argv[argv.index("--h1") + 1],
            "/reads/hic-L1-R1.fastq.gz,/reads/hic-L2-R1.fastq.gz",
        )
        self.assertEqual(
            argv[argv.index("--h2") + 1],
            "/reads/hic-L1-R2.fastq.gz,/reads/hic-L2-R2.fastq.gz",
        )
        self.assertEqual(
            argv[-2:],
            ["/reads/hifi-a.fastq.gz", "/reads/hifi-b.fastq.gz"],
        )

    def test_hifiasm_ont_renders_multiple_primary_reads_with_fixed_ont_mode(self):
        catalog = load_parameter_catalog(HIFIASM_ONT_METHOD)
        resolved = resolve_assembly_parameters(
            catalog=catalog,
            snapshot=_snapshot(HIFIASM_ONT_METHOD),
            approved_l2={"kmer_length": 31},
            l0_executor_values={
                "assembly_output_prefix": "/work/hifiasm-ont/out",
                "assembly_input_reads": [
                    "/reads/ont-a.fastq.gz",
                    "/reads/ont-b.fastq.gz",
                ],
                "assembly_threads": 16,
            },
        )

        argv = render_assembly_cli(catalog, resolved)

        self.assertIn("--ont", argv)
        self.assertTrue({"--ul", "--h1", "--h2"}.isdisjoint(argv))
        self.assertEqual(
            argv[-2:],
            ["/reads/ont-a.fastq.gz", "/reads/ont-b.fastq.gz"],
        )

    def test_parameter_sources_payload_contains_all_six_views(self):
        catalog = load_parameter_catalog(FLYE_METHOD)
        resolved = resolve_assembly_parameters(
            catalog=catalog,
            snapshot=_snapshot(FLYE_METHOD),
            approved_l2={"min_overlap": 5000},
            l0_executor_values={
                "assembly_method_read_type": "nano-hq",
                "assembly_input_reads": ["/reads/a.fastq.gz"],
                "assembly_output_dir": "/work/flye",
                "assembly_threads": 8,
            },
        )

        self.assertEqual(
            set(parameter_sources_payload(resolved)),
            {
                "l0_executor",
                "l1_facts",
                "l2_approved",
                "effective_parameters",
                "effective_source_by_parameter",
                "applications",
            },
        )


class AssemblyAdapterV3Tests(unittest.TestCase):
    def test_execution_contract_has_no_legacy_field_fallback(self):
        output = {
            "env": {"PATH": "/legacy"},
            "flye_parameters": {"threads": 8},
        }

        with self.assertRaisesRegex(
            ValueError,
            "schema-3 execution contract is missing",
        ):
            assembly_support.load_assembly_execution_contract(
                output,
                method_id=FLYE_METHOD,
            )

    def test_execution_contract_freezes_l0_value_and_provenance(self):
        output = {
            "env": {"PATH": "/frozen"},
            "flye_parameters": {"threads": 8},
        }

        contract = assembly_support.build_assembly_execution_contract(
            output,
            method_id=FLYE_METHOD,
        )
        output["env"]["PATH"] = "/mutated"
        output["flye_parameters"]["threads"] = 64

        self.assertEqual(contract.schema_version, 3)
        self.assertEqual(contract.environment.values, {"PATH": "/frozen"})
        self.assertEqual(contract.threads.value, 8)
        self.assertEqual(contract.threads.source, "run_config")
        self.assertEqual(contract.l0_executor_values(), {"assembly_threads": 8})

    def test_execution_contract_rejects_falsey_non_mapping_sources(self):
        for output, field in (
            ({"env": []}, "env must be a mapping"),
            ({"flye_parameters": []}, "flye_parameters must be a mapping"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    assembly_support.build_assembly_execution_contract(
                        output,
                        method_id=FLYE_METHOD,
                    )

    def test_support_returns_the_strict_approved_v3_bundle(self):
        attempt = SimpleNamespace(attempt_id=0)
        bundle = object()
        with (
            patch.object(
                assembly_support,
                "current_parameter_attempt",
                return_value=attempt,
            ),
            patch.object(
                assembly_support,
                "load_approved_parameter_chain_v3",
                return_value=bundle,
            ) as load_bundle,
        ):
            loaded, error = assembly_support.load_current_approved_recommendation(
                {},
                {},
                method_id=FLYE_METHOD,
                label="Flye",
            )

        self.assertIs(loaded, bundle)
        self.assertIsNone(error)
        load_bundle.assert_called_once_with(
            {},
            attempt=attempt,
            expected_stage="assembly",
            expected_method_id=FLYE_METHOD,
        )

    def test_support_turns_v3_evidence_errors_into_stage_friendly_errors(self):
        with (
            patch.object(
                assembly_support,
                "current_parameter_attempt",
                return_value=SimpleNamespace(attempt_id=0),
            ),
            patch.object(
                assembly_support,
                "load_approved_parameter_chain_v3",
                side_effect=ParameterEvidenceError("broken chain"),
            ),
        ):
            loaded, error = assembly_support.load_current_approved_recommendation(
                {},
                {},
                method_id=FLYE_METHOD,
                label="Flye",
            )

        self.assertIsNone(loaded)
        self.assertEqual(
            error,
            "Flye recommendation evidence is invalid: broken chain",
        )

    def test_all_freeze_adapters_use_snapshot_read_type_and_persist_six_views(self):
        cases = (
            (
                flye_adapter,
                "freeze_flye_attempt",
                FLYE_METHOD,
                "flye_execution_planned",
                "flye_parameters",
                "flye",
                "pacbio-raw",
                "nano-hq",
                "nano-hq",
            ),
            (
                nextdenovo_adapter,
                "freeze_nextdenovo_attempt",
                NEXTDENOVO_METHOD,
                "nextdenovo_execution_planned",
                "nextdenovo_parameters",
                "nextdenovo",
                "clr",
                "ont",
                "ont",
            ),
            (
                hifiasm_adapter,
                "freeze_hifiasm_attempt",
                HIFIASM_METHOD,
                "hifiasm_execution_planned",
                "hifiasm_parameters",
                "hifiasm",
                "ont",
                None,
                "pacbio_hifi",
            ),
            (
                hifiasm_ont_adapter,
                "freeze_hifiasm_ont_attempt",
                HIFIASM_ONT_METHOD,
                "hifiasm_ont_execution_planned",
                "hifiasm_ont_parameters",
                "hifiasm_ont",
                "hifi",
                None,
                "ont_raw",
            ),
        )

        for (
            adapter,
            freeze_name,
            method_id,
            status,
            parameter_key,
            prior_key,
            mutable_read_type,
            expected_method_read_type,
            expected_read_profile,
        ) in cases:
            with (
                self.subTest(method_id=method_id),
                tempfile.TemporaryDirectory() as tmp,
            ):
                run_dir = Path(tmp) / "runs" / "s1"
                ensure_run_layout(SessionPaths.from_run_dir(run_dir))
                output = {
                    "status": status,
                    "input_ref": "/reads/long.fastq.gz",
                    "input_type": "ont_raw",
                    parameter_key: {
                        "read_type": mutable_read_type,
                        "threads": 6,
                    },
                    "read_type_prior": {prior_key: mutable_read_type},
                    "assembly_input_member_ids": {"primary": ["fixture-member-0"]},
                }
                contract = assembly_support.build_assembly_execution_contract(
                    output,
                    method_id=method_id,
                )
                output[assembly_support.ASSEMBLY_EXECUTION_CONTRACT_KEY] = (
                    contract.model_dump(mode="json")
                )
                output[parameter_key]["threads"] = 99
                output["env"] = {"PATH": "/mutable/not-frozen"}
                state = _assembly_state(run_dir, output)
                bundle = SimpleNamespace(
                    attempt=SimpleNamespace(attempt_id=0),
                    chain=SimpleNamespace(
                        catalog=load_parameter_catalog(method_id),
                        input_snapshot=_snapshot(method_id),
                    ),
                    approved_l2={},
                )

                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            adapter,
                            "load_descriptor_registry",
                            return_value=SimpleNamespace(methods={method_id: object()}),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            adapter,
                            "resolve_assembly_read_paths",
                            return_value=[Path("/reads/long.fastq.gz")],
                        )
                    )
                    if adapter is hifiasm_ont_adapter:
                        stack.enter_context(
                            patch.object(
                                adapter,
                                "resolve_long_read_members",
                                return_value=[
                                    SimpleNamespace(
                                        path="/reads/long.fastq.gz", format="fastq"
                                    )
                                ],
                            )
                        )
                    stack.enter_context(
                        patch.object(
                            adapter,
                            "build_read_input_dependencies",
                            return_value=[],
                        )
                    )
                    env_resolver = stack.enter_context(
                        patch.object(
                            adapter,
                            "resolve_method_env",
                            return_value=(None, [], None),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            adapter,
                            "load_current_approved_recommendation",
                            return_value=(bundle, None),
                        )
                    )
                    resolver = stack.enter_context(
                        patch.object(
                            adapter,
                            "resolve_assembly_parameters",
                            wraps=resolve_assembly_parameters,
                        )
                    )
                    frozen = stack.enter_context(
                        patch.object(
                            adapter,
                            "freeze_parameterized_attempt",
                            return_value={"frozen": method_id},
                        )
                    )
                    result = getattr(adapter, freeze_name)(state)

                self.assertEqual(result, {"frozen": method_id})
                self.assertEqual(
                    env_resolver.call_args.args[0],
                    {"env": {}},
                )
                l0 = resolver.call_args.kwargs["l0_executor_values"]
                if expected_method_read_type is None:
                    self.assertNotIn("assembly_method_read_type", l0)
                else:
                    self.assertEqual(
                        l0["assembly_method_read_type"],
                        expected_method_read_type,
                    )
                self.assertEqual(l0["assembly_threads"], 6)
                payload = frozen.call_args.kwargs["frozen_parameters_payload"]
                self.assertEqual(
                    set(payload["parameter_sources"]),
                    {
                        "l0_executor",
                        "l1_facts",
                        "l2_approved",
                        "effective_parameters",
                        "effective_source_by_parameter",
                        "applications",
                    },
                )
                metadata = frozen.call_args.kwargs["frozen_attempt_metadata"]
                self.assertEqual(metadata["read_profile"], expected_read_profile)
                self.assertEqual(metadata["threads"], 6)

    def test_all_freeze_adapters_leave_default_threads_to_the_catalog(self):
        cases = (
            (
                flye_adapter,
                "freeze_flye_attempt",
                FLYE_METHOD,
                "flye_execution_planned",
                "flye_parameters",
            ),
            (
                nextdenovo_adapter,
                "freeze_nextdenovo_attempt",
                NEXTDENOVO_METHOD,
                "nextdenovo_execution_planned",
                "nextdenovo_parameters",
            ),
            (
                hifiasm_adapter,
                "freeze_hifiasm_attempt",
                HIFIASM_METHOD,
                "hifiasm_execution_planned",
                "hifiasm_parameters",
            ),
            (
                hifiasm_ont_adapter,
                "freeze_hifiasm_ont_attempt",
                HIFIASM_ONT_METHOD,
                "hifiasm_ont_execution_planned",
                "hifiasm_ont_parameters",
            ),
        )

        for adapter, freeze_name, method_id, status, parameter_key in cases:
            with (
                self.subTest(method_id=method_id),
                tempfile.TemporaryDirectory() as tmp,
            ):
                run_dir = Path(tmp) / "runs" / "s1"
                ensure_run_layout(SessionPaths.from_run_dir(run_dir))
                output = {
                    "status": status,
                    "input_ref": "/reads/long.fastq.gz",
                    "input_type": "ont_raw",
                    parameter_key: {"read_type": "mutable-value"},
                    "read_type_prior": {"mutable": "mutable-value"},
                    "assembly_input_member_ids": {"primary": ["fixture-member-0"]},
                }
                contract = assembly_support.build_assembly_execution_contract(
                    output,
                    method_id=method_id,
                )
                output[assembly_support.ASSEMBLY_EXECUTION_CONTRACT_KEY] = (
                    contract.model_dump(mode="json")
                )
                output[parameter_key]["threads"] = 99
                state = _assembly_state(run_dir, output)
                bundle = SimpleNamespace(
                    attempt=SimpleNamespace(attempt_id=0),
                    chain=SimpleNamespace(
                        catalog=load_parameter_catalog(method_id),
                        input_snapshot=_snapshot(method_id),
                    ),
                    approved_l2={},
                )

                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            adapter,
                            "load_descriptor_registry",
                            return_value=SimpleNamespace(methods={method_id: object()}),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            adapter,
                            "resolve_assembly_read_paths",
                            return_value=[Path("/reads/long.fastq.gz")],
                        )
                    )
                    if adapter is hifiasm_ont_adapter:
                        stack.enter_context(
                            patch.object(
                                adapter,
                                "resolve_long_read_members",
                                return_value=[
                                    SimpleNamespace(
                                        path="/reads/long.fastq.gz", format="fastq"
                                    )
                                ],
                            )
                        )
                    stack.enter_context(
                        patch.object(
                            adapter,
                            "build_read_input_dependencies",
                            return_value=[],
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            adapter,
                            "resolve_method_env",
                            return_value=(None, [], None),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            adapter,
                            "load_current_approved_recommendation",
                            return_value=(bundle, None),
                        )
                    )
                    resolver = stack.enter_context(
                        patch.object(
                            adapter,
                            "resolve_assembly_parameters",
                            wraps=resolve_assembly_parameters,
                        )
                    )
                    frozen = stack.enter_context(
                        patch.object(
                            adapter,
                            "freeze_parameterized_attempt",
                            return_value={"frozen": method_id},
                        )
                    )
                    result = getattr(adapter, freeze_name)(state)

                self.assertEqual(result, {"frozen": method_id})
                l0 = resolver.call_args.kwargs["l0_executor_values"]
                self.assertNotIn("assembly_threads", l0)
                sources = frozen.call_args.kwargs["frozen_parameters_payload"][
                    "parameter_sources"
                ]
                self.assertEqual(sources["l0_executor"]["threads"], 32)
                self.assertEqual(sources["effective_parameters"]["threads"], 32)
                self.assertEqual(
                    sources["effective_source_by_parameter"]["threads"],
                    "l0",
                )
                metadata = frozen.call_args.kwargs["frozen_attempt_metadata"]
                self.assertEqual(metadata["threads"], 32)


if __name__ == "__main__":
    unittest.main()
