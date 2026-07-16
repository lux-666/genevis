"""Tests for the selected, AutoHiC-only Hi-C scaffolding stage."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.control.lifecycle import ResumeValidationError
from app.graph.routing import next_stage_name
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import (
    ArtifactRef,
    ParameterAttempt,
    ParameterRecommendationCursor,
)
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.hic_scaffolding.builder import build_stage
from app.stages.hic_scaffolding.method_application.autohic import (
    freeze_autohic_attempt,
)
from app.stages.hic_scaffolding.nodes import (
    apply_hic_scaffolding_resume_decision,
    resolve_hic_inputs,
)
from app.stages.hic_scaffolding.parameters.catalog import (
    load_autohic_parameter_catalog,
)
from app.stages.hic_scaffolding.parameters.workflow import (
    apply_autohic_parameter_approval_decision,
    recommend_autohic_parameters,
)
from tests.data_v2 import v2_run_dir

AUTOHIC_METHOD = "hic_scaffolding.candidate.autohic"


class _FakeModel:
    def __init__(self, payload: dict):
        self.payload = payload
        self.messages: list[dict[str, str]] | None = None

    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(content=json.dumps(self.payload))


class HiCScaffoldingDescriptorTests(unittest.TestCase):
    def test_registry_and_catalog_expose_only_autohic(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        self.assertEqual(registry.by_stage["hic_scaffolding"], [AUTOHIC_METHOD])
        self.assertEqual(registry.methods[AUTOHIC_METHOD].parameter_catalog, "autohic")
        self.assertFalse(any("yahs" in method.lower() for method in registry.methods))
        catalog = load_autohic_parameter_catalog()
        self.assertEqual(
            {
                name
                for name, specification in catalog.parameters.items()
                if specification.level == 2
            },
            {"restriction_enzyme"},
        )


class HiCScaffoldingStageTests(unittest.TestCase):
    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "s1", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _state(self, tmpdir: str, registry_ref: str | None) -> dict:
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(tmpdir, "s1")),
        )
        state["current_stage"] = "hic_scaffolding"
        state["asset_registry_ref"] = registry_ref
        return state

    def _inputs(
        self,
        tmpdir: str,
        *,
        read_count: int = 2,
    ) -> tuple[Path, list[Path], Path, Path]:
        root = Path(tmpdir)
        assembly = root / "adopted_contigs.fasta"
        assembly.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
        reads: list[Path] = []
        for index in range(read_count):
            mate = "R1" if index % 2 == 0 else "R2"
            read = root / f"lane_{index // 2 + 1}_{mate}.fastq.gz"
            read.write_text(f"@read{index + 1}\nACGT\n+\n!!!!\n", encoding="utf-8")
            reads.append(read)
        error_model = root / "error_model.pth"
        chromosome_model = root / "chr_model.pth"
        error_model.write_text("error model\n", encoding="utf-8")
        chromosome_model.write_text("chromosome model\n", encoding="utf-8")
        return assembly, reads, error_model, chromosome_model

    def _registry_for_inputs(
        self,
        tmpdir: str,
        assembly: Path,
        reads: list[Path],
        *,
        library_metadata: dict | None = None,
    ) -> Path:
        assets = [
            {
                "asset_id": "s1:assembly_fasta_registered:adopted",
                "asset_type": "assembly_fasta_registered",
                "path": str(assembly),
            }
        ]
        assets.extend(
            {
                "asset_id": f"s1:hic_reads_registered:{path.name}",
                "asset_type": "hic_reads_registered",
                "path": str(path),
                "metadata": library_metadata or {},
            }
            for path in reads
        )
        return self._write_registry(tmpdir, assets=assets)

    def _configured_state(
        self,
        tmpdir: str,
        assembly: Path,
        reads: list[Path],
        *,
        target_level: str = "chromosome",
    ) -> dict:
        state = self._state(
            tmpdir,
            str(
                self._registry_for_inputs(
                    tmpdir,
                    assembly,
                    reads,
                    library_metadata={"library_protocol": "in_situ_hic"},
                )
            ),
        )
        state["stage_outputs"] = {
            "inventory": {"target_assembly_level": target_level},
            "assembly_review": {
                "status": "completed",
                "final_assembly_ref": str(assembly),
            },
            "hic_scaffolding": {
                "execution_mode": "local",
                # This is evidence for the LLM, not an approved parameter.
                "autohic_parameters": {
                    "restriction_enzyme": "HindIII",
                    "threads": 2,
                    "python_command": "python",
                },
            },
        }
        return state

    def _fake_autohic_environment(
        self,
        tmpdir: str,
        error_model: Path,
        chromosome_model: Path,
        *,
        fail: bool = False,
    ) -> Path:
        root = Path(tmpdir)
        autohic_root = root / "AutoHiC"
        (autohic_root / "src" / "common").mkdir(parents=True)
        (autohic_root / "src" / "models" / "cfgs").mkdir(parents=True)
        (autohic_root / "src" / "common" / "run.sh").write_text(
            "# fake pipeline\n", encoding="utf-8"
        )
        (autohic_root / "src" / "models" / "cfgs" / "error_model.py").write_text(
            "# fake model cfg\n", encoding="utf-8"
        )
        entry = autohic_root / "autohic.py"
        entry.write_text(
            "import pathlib, sys\n"
            "cfg_path = pathlib.Path(sys.argv[sys.argv.index('-c') + 1])\n"
            "cfg = dict(line.strip().split('=', 1) for line in cfg_path.read_text().splitlines() if line.strip())\n"
            "if " + repr(fail) + ":\n"
            "    print('fake AutoHiC failed', file=sys.stderr)\n"
            "    raise SystemExit(17)\n"
            "assert pathlib.Path(cfg['REFERENCE_GENOME']).is_file()\n"
            "fastqs = sorted(path.name for path in (pathlib.Path(cfg['FASTQ_DIR']) / 'fastq').iterdir())\n"
            "assert fastqs == ['hic_001_R1.fastq.gz', 'hic_001_R2.fastq.gz']\n"
            "assert pathlib.Path(cfg['ERROR_PRETRAINED_MODEL']).is_file()\n"
            "assert pathlib.Path(cfg['CHR_PRETRAINED_MODEL']).is_file()\n"
            "top = pathlib.Path(cfg['RESULT_DIR']) / cfg['JOB_NAME']\n"
            "chromosome = top / 'autohic_results' / 'chromosome'\n"
            "chromosome.mkdir(parents=True)\n"
            "(top / 'contigs_autohic.fasta').write_text('>chr1\\nACGTACGT\\n')\n"
            "(chromosome / 'contigs.final.hic').write_text('hic\\n')\n"
            "(chromosome / 'contigs.final.assembly').write_text('assembly\\n')\n"
            "(top / 'result.html').write_text('<html>AutoHiC report</html>\\n')\n"
            "print('fake AutoHiC ok')\n",
            encoding="utf-8",
        )

        juicer_root = root / "juicer"
        (juicer_root / "CPU" / "common").mkdir(parents=True)
        (juicer_root / "misc").mkdir(parents=True)
        (juicer_root / "scripts" / "common").mkdir(parents=True)
        for relative in [
            "CPU/juicer.sh",
            "CPU/common/chimeric_blacklist.awk",
            "misc/generate_site_positions.py",
            "scripts/common/juicer_tools.jar",
        ]:
            (juicer_root / relative).write_text("stub\n", encoding="utf-8")

        three_d_dna_root = root / "3d-dna"
        (three_d_dna_root / "edit").mkdir(parents=True)
        (three_d_dna_root / "visualize").mkdir(parents=True)
        for relative in [
            "run-asm-pipeline.sh",
            "run-asm-pipeline-post-review.sh",
            "edit/apply-edits-prep-for-next-round.sh",
            "visualize/run-assembly-visualizer.sh",
        ]:
            (three_d_dna_root / relative).write_text("stub\n", encoding="utf-8")

        envs_yaml = root / "envs.yaml"
        envs_yaml.write_text(
            "default:\n"
            "  conda_bin: conda\n"
            "  fallback_env: base\n"
            "packages:\n"
            "  autohic:\n"
            f"    install_root: {autohic_root}\n"
            "    canary_paths: [autohic.py, src/common/run.sh, src/models/cfgs/error_model.py]\n"
            "    models:\n"
            f"      error: {error_model}\n"
            f"      chromosome: {chromosome_model}\n"
            "  juicer:\n"
            f"    install_root: {juicer_root}\n"
            "    canary_paths: [CPU/juicer.sh, CPU/common/chimeric_blacklist.awk, misc/generate_site_positions.py, scripts/common/juicer_tools.jar]\n"
            "  three_d_dna:\n"
            f"    install_root: {three_d_dna_root}\n"
            "    canary_paths: [run-asm-pipeline.sh, run-asm-pipeline-post-review.sh, edit/apply-edits-prep-for-next-round.sh, visualize/run-assembly-visualizer.sh]\n"
            "methods:\n"
            f"  {AUTOHIC_METHOD}:\n"
            "    conda_env: base\n"
            "    package_tools: [autohic, juicer, three_d_dna]\n",
            encoding="utf-8",
        )
        return envs_yaml

    def _run_to_parameter_gate(
        self,
        state: dict,
        *,
        enzyme: str = "MboI",
    ) -> dict:
        model = _FakeModel(
            {
                "parameters": {"restriction_enzyme": enzyme},
                "summary": "Library metadata supports this enzyme.",
            }
        )
        with patch(
            "app.llm.parameters.hic.recommender.build_chat_model",
            return_value=model,
        ):
            return build_stage().invoke(state)

    def _approve_parameters(self, blocked: dict) -> dict:
        interrupt = blocked["active_interrupt"]
        bindings = {
            key: interrupt.get(key)
            for key in (
                "attempt_id",
                "task",
                "previous_attempt_id",
                "recommendation_hash",
            )
        }
        update = apply_autohic_parameter_approval_decision(
            blocked,
            ResumeReviewRequest(
                session_id="s1",
                stage="hic_scaffolding",
                gate_id="parameter_approval",
                decision="approve_parameters",
                notes=["approved AutoHiC enzyme"],
                metadata=bindings,
                review_request_id=interrupt["review_request_id"],
                review_request_hash=interrupt["request_hash"],
            ),
        )
        return {**blocked, **update}

    def test_graph_has_direct_autohic_without_selection_or_skip_nodes(self):
        nodes = set(build_stage().get_graph().nodes)
        self.assertIn("autohic", nodes)
        self.assertIn("recommend_autohic_parameters", nodes)
        self.assertIn("chromosome_assembly_review", nodes)
        for removed in (
            "select_hic_mode",
            "write_hic_skip",
            "plan_hic_scaffold",
            "yahs",
            "juicer_3ddna",
        ):
            self.assertNotIn(removed, nodes)

    def test_selected_stage_missing_assembly_blocks_requirements(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                str(self._write_registry(tmpdir, assets=[])),
            )
            state["stage_outputs"] = {"hic_scaffolding": {"execution_mode": "local"}}
            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "blocked_missing_requirements")
            self.assertIn("pre_hic_assembly_ref", hic["missing_requirements"])
            self.assertEqual(result["supervision_state"], "failed_terminal")

    def test_selected_stage_missing_reads_blocks_instead_of_skipping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, _, _, _ = self._inputs(tmpdir, read_count=0)
            state = self._configured_state(tmpdir, assembly, [])
            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "blocked_missing_requirements")
            self.assertIn("paired hic_read_paths (R1/R2)", hic["missing_requirements"])
            self.assertNotIn("mode", hic)
            self.assertNotIn("skip_record_ref", hic)

    def test_target_level_does_not_create_a_stage_local_skip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._configured_state(
                tmpdir,
                assembly,
                reads,
                target_level="contig",
            )
            result = self._run_to_parameter_gate(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(
                result["active_interrupt"]["gate_id"], "parameter_approval"
            )
            self.assertNotIn("mode", result["stage_outputs"]["hic_scaffolding"])

    def test_llm_directly_recommends_only_enzyme_and_always_blocks_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads)
            result = self._run_to_parameter_gate(state, enzyme="MboI")

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "blocked_human")
            self.assertEqual(
                result["active_interrupt"]["gate_id"], "parameter_approval"
            )
            self.assertFalse(hic["parameter_attempts"])
            recommendation_path = (
                Path(result["data_dir"])
                / "artifacts"
                / "hic_scaffolding"
                / "parameter_recommendations"
                / "000"
                / "recommendation.json"
            )
            recommendation = json.loads(
                recommendation_path.read_text(encoding="utf-8")
            )["recommendation"]
            self.assertEqual(
                recommendation["parameters"],
                {"restriction_enzyme": "MboI"},
            )
            self.assertNotIn("params", recommendation)
            self.assertNotIn("confidence", json.dumps(recommendation))

    def test_unknown_enzyme_string_still_reaches_human_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            result = self._run_to_parameter_gate(
                self._configured_state(tmpdir, assembly, reads),
                enzyme="definitely-not-an-enzyme",
            )

            self.assertEqual(
                result["stage_outputs"]["hic_scaffolding"]["status"],
                "blocked_human",
            )
            request_ref = result["active_interrupt"]["request_ref"]
            request = json.loads(
                (Path(result["data_dir"]) / request_ref["path_ref"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                request["payload"]["parameters"],
                {"restriction_enzyme": "definitely-not-an-enzyme"},
            )

    def test_model_request_is_exact_and_excludes_run_locators_and_raw_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads)
            registry_path = Path(state["asset_registry_ref"])
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            for asset in registry["assets"]:
                if asset["asset_type"] == "hic_reads_registered":
                    asset["metadata"]["arbitrary_raw_metadata"] = "must-not-leak"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            model = _FakeModel(
                {
                    "parameters": {"restriction_enzyme": "MboI"},
                    "summary": "Use the supplied library facts.",
                }
            )
            with patch(
                "app.llm.parameters.hic.recommender.build_chat_model",
                return_value=model,
            ):
                result = build_stage().invoke(state)

            request_path = (
                Path(result["data_dir"])
                / "artifacts"
                / "hic_scaffolding"
                / "parameter_recommendations"
                / "000"
                / "model_request.json"
            )
            persisted = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["messages"], model.messages)
            self.assertNotIn("request_id", persisted)
            rendered = "\n".join(
                message["content"] for message in persisted["messages"]
            )
            self.assertIn('"library_protocols"', rendered)
            request_dto = json.loads(persisted["messages"][1]["content"])
            self.assertEqual(
                request_dto["input_facts"]["provided_restriction_enzyme"],
                "HindIII",
            )
            self.assertEqual(
                persisted["messages"][1]["content"],
                json.dumps(
                    request_dto,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            for forbidden in (
                "session_id",
                "pre_hic_assembly_ref",
                "hic_read_refs",
                "hic_read_paths",
                "hic_library_metadata",
                "asset_id",
                "arbitrary_raw_metadata",
                "must-not-leak",
                str(assembly),
                *(str(read) for read in reads),
            ):
                self.assertNotIn(forbidden, rendered)

            profile_path = request_path.with_name("input_profile.json")
            full_profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertNotIn("session_id", full_profile)
            self.assertIn("arbitrary_raw_metadata", json.dumps(full_profile))

    def test_invalid_llm_shape_fails_without_an_approval_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads)
            model = _FakeModel(
                {
                    "parameters": {"restriction_enzyme": "MboI"},
                    "confidence": 0.9,
                    "alternatives": ["DpnII"],
                }
            )
            with patch(
                "app.llm.parameters.hic.recommender.build_chat_model",
                return_value=model,
            ):
                result = build_stage().invoke(state)
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertEqual(
                result["stage_outputs"]["hic_scaffolding"]["failure_kind"],
                "parameter_recommendation",
            )

    def test_stale_checkpoint_reuses_persisted_autohic_response(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads)
            resolved = resolve_hic_inputs(state)
            resolved_state = {**state, "stage_outputs": resolved["stage_outputs"]}
            with patch(
                "app.llm.parameters.hic.recommender.build_chat_model",
                return_value=_FakeModel(
                    {
                        "parameters": {"restriction_enzyme": "MboI"},
                        "summary": "Use the completed response.",
                    }
                ),
            ):
                first = recommend_autohic_parameters(resolved_state)

            with patch(
                "app.llm.parameters.hic.recommender.build_chat_model",
                side_effect=AssertionError(
                    "the model must not be called during recovery"
                ),
            ):
                replay = recommend_autohic_parameters(resolved_state)

            first_cursor = ParameterRecommendationCursor.model_validate(
                first["stage_outputs"]["hic_scaffolding"][
                    "current_parameter_recommendation"
                ]
            )
            replay_cursor = ParameterRecommendationCursor.model_validate(
                replay["stage_outputs"]["hic_scaffolding"][
                    "current_parameter_recommendation"
                ]
            )
            self.assertEqual(
                replay["stage_outputs"]["hic_scaffolding"]["status"],
                "blocked_human",
            )
            self.assertEqual(replay_cursor.manifest_ref, first_cursor.manifest_ref)
            self.assertEqual(
                replay_cursor.review_request_ref,
                first_cursor.review_request_ref,
            )
            self.assertEqual(
                replay_cursor.review_request_id,
                first_cursor.review_request_id,
            )

    def test_parameter_approval_requires_hash_bound_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            blocked = self._run_to_parameter_gate(
                self._configured_state(tmpdir, assembly, reads)
            )
            interrupt = blocked["active_interrupt"]
            with self.assertRaisesRegex(
                ResumeValidationError,
                "metadata missing bindings",
            ):
                apply_autohic_parameter_approval_decision(
                    blocked,
                    ResumeReviewRequest(
                        session_id="s1",
                        stage="hic_scaffolding",
                        gate_id="parameter_approval",
                        decision="approve_parameters",
                        review_request_id=interrupt["review_request_id"],
                        review_request_hash=interrupt["request_hash"],
                    ),
                )

    def test_parameter_approval_fails_closed_when_manifest_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            blocked = self._run_to_parameter_gate(
                self._configured_state(tmpdir, assembly, reads)
            )
            cursor = blocked["stage_outputs"]["hic_scaffolding"][
                "current_parameter_recommendation"
            ]
            manifest = Path(blocked["data_dir"]) / cursor["manifest_ref"]["path_ref"]
            manifest.unlink()

            with self.assertRaisesRegex(
                ResumeValidationError,
                "recommendation evidence is missing or changed",
            ):
                self._approve_parameters(blocked)

    def test_parameter_approval_fails_closed_when_recommendation_is_tampered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            blocked = self._run_to_parameter_gate(
                self._configured_state(tmpdir, assembly, reads)
            )
            cursor = blocked["stage_outputs"]["hic_scaffolding"][
                "current_parameter_recommendation"
            ]
            manifest_path = (
                Path(blocked["data_dir"]) / cursor["manifest_ref"]["path_ref"]
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            recommendation_path = (
                Path(blocked["data_dir"])
                / manifest["refs"]["recommendation_ref"]["path_ref"]
            )
            recommendation_path.write_text(
                recommendation_path.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ResumeValidationError,
                "recommendation evidence is missing or changed",
            ):
                self._approve_parameters(blocked)

    def test_freeze_checkpoint_contains_only_refs_and_compact_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(
                tmpdir,
                error_model,
                chromosome_model,
            )
            approved = self._approve_parameters(
                self._run_to_parameter_gate(
                    self._configured_state(tmpdir, assembly, reads),
                    enzyme="MboI",
                )
            )
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                update = freeze_autohic_attempt(approved)
                replay_update = freeze_autohic_attempt(approved)
            frozen_state = {**approved, **update}
            hic = frozen_state["stage_outputs"]["hic_scaffolding"]

            self.assertEqual(hic["status"], "autohic_frozen")
            command_cursor = ArtifactRef.model_validate(hic["autohic_frozen_attempt"])
            replay_cursor = ArtifactRef.model_validate(
                replay_update["stage_outputs"]["hic_scaffolding"][
                    "autohic_frozen_attempt"
                ]
            )
            self.assertEqual(replay_cursor, command_cursor)
            self.assertEqual(
                set(hic["autohic_frozen_attempt"]),
                {
                    "schema_version",
                    "artifact_id",
                    "path_ref",
                    "sha256",
                    "size_bytes",
                    "artifact_type",
                },
            )
            attempt = ParameterAttempt.model_validate(hic["parameter_attempts"][0])
            self.assertEqual(attempt.status, "frozen")
            self.assertEqual(attempt.command_ref, command_cursor)
            self.assertIsNotNone(attempt.frozen_parameters_ref)

            command = json.loads(
                (Path(frozen_state["data_dir"]) / command_cursor.path_ref).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                command["frozen_parameters_ref"]["path_ref"],
                attempt.frozen_parameters_ref.path_ref,
            )
            self.assertEqual(
                command["frozen_attempt"]["script_path"],
                str(
                    Path(frozen_state["data_dir"])
                    / "work"
                    / "hic_scaffolding"
                    / "autohic"
                    / "run_autohic.sh"
                ),
            )
            for ref_name in ("config_ref", "script_ref"):
                ref = ArtifactRef.model_validate(command[ref_name])
                self.assertTrue(
                    (Path(frozen_state["data_dir"]) / ref.path_ref).is_file()
                )

            frozen_parameters = json.loads(
                (
                    Path(frozen_state["data_dir"])
                    / attempt.frozen_parameters_ref.path_ref
                ).read_text(encoding="utf-8")
            )["parameters"]
            self.assertEqual(frozen_parameters["restriction_enzyme"], "MboI")
            self.assertEqual(frozen_parameters["threads"], 2)
            self.assertEqual(frozen_parameters["python_command"], "python")

    def test_approved_autohic_runs_then_blocks_at_separate_adoption_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(
                tmpdir,
                error_model,
                chromosome_model,
            )
            blocked = self._run_to_parameter_gate(
                self._configured_state(tmpdir, assembly, reads),
                enzyme="MboI",
            )
            approved = self._approve_parameters(blocked)
            self.assertEqual(
                approved["stage_outputs"]["hic_scaffolding"]["status"],
                "parameters_approved",
            )
            frozen_attempt = approved["stage_outputs"]["hic_scaffolding"][
                "parameter_attempts"
            ][0]
            self.assertEqual(frozen_attempt["status"], "recommended")
            self.assertIsNone(frozen_attempt["frozen_parameters_ref"])
            self.assertEqual(
                approved["stage_outputs"]["hic_scaffolding"]["autohic_parameters"][
                    "restriction_enzyme"
                ],
                "HindIII",
            )
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(approved)

            hic = result["stage_outputs"]["hic_scaffolding"]
            candidate = hic["method_candidates"][AUTOHIC_METHOD]
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(result["active_interrupt"]["gate_id"], "hic_scaffolding")
            self.assertEqual(candidate["status"], "executed")
            executed_attempt = hic["parameter_attempts"][0]
            self.assertEqual(executed_attempt["status"], "executed")
            self.assertEqual(
                executed_attempt["frozen_parameters_ref"]["path_ref"],
                "artifacts/hic_scaffolding/attempts/000/frozen_parameters.json",
            )
            self.assertEqual(
                executed_attempt["command_ref"]["path_ref"],
                "artifacts/hic_scaffolding/attempts/000/command.json",
            )
            self.assertEqual(
                executed_attempt["execution_ref"]["path_ref"],
                "artifacts/hic_scaffolding/attempts/000/execution.json",
            )
            self.assertIsNone(hic.get("autohic_frozen_attempt"))
            self.assertIsNone(hic.get("autohic_execution_result"))
            for key in (
                "scaffold_fasta",
                "contact_map",
                "assembly_file",
                "html_report",
            ):
                self.assertTrue(Path(candidate[key]).is_file(), key)

            config = (
                Path(result["data_dir"])
                / "work"
                / "hic_scaffolding"
                / "autohic"
                / "cfg-autohic.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("ENZYME=MboI", config)
            self.assertIn(f"ERROR_PRETRAINED_MODEL={error_model.resolve()}", config)
            self.assertIn(
                f"CHR_PRETRAINED_MODEL={chromosome_model.resolve()}",
                config,
            )

            command = json.loads(
                (
                    Path(result["data_dir"])
                    / executed_attempt["command_ref"]["path_ref"]
                ).read_text(encoding="utf-8")
            )
            for ref_name in ("config_ref", "script_ref"):
                ref = ArtifactRef.model_validate(command[ref_name])
                self.assertTrue((Path(result["data_dir"]) / ref.path_ref).is_file())
            review_bundle = json.loads(
                Path(candidate["review_bundle_ref"]).read_text(encoding="utf-8")
            )
            self.assertNotIn("attempt_metadata", review_bundle)
            self.assertNotIn("stdout_path", review_bundle)
            self.assertNotIn("stderr_path", review_bundle)
            self.assertEqual(
                review_bundle["execution_ref"]["path_ref"],
                executed_attempt["execution_ref"]["path_ref"],
            )
            execution_record = json.loads(
                (
                    Path(result["data_dir"])
                    / executed_attempt["execution_ref"]["path_ref"]
                ).read_text(encoding="utf-8")
            )
            self.assertNotIn("stdout_path", execution_record["result"])
            self.assertNotIn("stderr_path", execution_record["result"])
            self.assertIn("stdout_ref", execution_record["result"])
            self.assertIn("stderr_ref", execution_record["result"])
            self.assertNotIn("output_paths", execution_record["result"])
            artifact_types = {
                json.loads(line)["artifact_type"]
                for line in (
                    Path(result["data_dir"]) / "artifacts" / "index.jsonl"
                )
                .read_text(encoding="utf-8")
                .splitlines()
            }
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)

            interrupt = result["active_interrupt"]
            adopted = apply_hic_scaffolding_resume_decision(
                result,
                ResumeReviewRequest(
                    session_id="s1",
                    stage="hic_scaffolding",
                    gate_id="hic_scaffolding",
                    decision="accept_candidate",
                    notes=["accepted AutoHiC chromosome assembly"],
                    review_request_id=interrupt["review_request_id"],
                    review_request_hash=interrupt["request_hash"],
                ),
            )
            self.assertEqual(
                adopted["current_stage"],
                next_stage_name("hic_scaffolding"),
            )
            self.assertEqual(
                adopted["stage_outputs"]["hic_scaffolding"][
                    "final_assembly_for_downstream"
                ],
                candidate["scaffold_fasta"],
            )
            decision_ref = adopted["stage_outputs"]["hic_scaffolding"][
                "review_decision_artifact_ref"
            ]
            decision_record = json.loads(
                (Path(result["data_dir"]) / decision_ref["path_ref"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                decision_record["request_ref"]["sha256"],
                interrupt["request_hash"],
            )

    def test_final_review_can_keep_prior_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(
                tmpdir,
                error_model,
                chromosome_model,
            )
            approved = self._approve_parameters(
                self._run_to_parameter_gate(
                    self._configured_state(tmpdir, assembly, reads)
                )
            )
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(approved)
            interrupt = result["active_interrupt"]
            kept = apply_hic_scaffolding_resume_decision(
                result,
                ResumeReviewRequest(
                    session_id="s1",
                    stage="hic_scaffolding",
                    gate_id="hic_scaffolding",
                    decision="keep_prior_assembly",
                    review_request_id=interrupt["review_request_id"],
                    review_request_hash=interrupt["request_hash"],
                ),
            )
            self.assertEqual(
                kept["stage_outputs"]["hic_scaffolding"][
                    "final_assembly_for_downstream"
                ],
                str(assembly),
            )

    def test_missing_models_after_approval_is_requirements_block_not_execution_failure(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            chromosome_model.unlink()
            envs_yaml = self._fake_autohic_environment(
                tmpdir,
                error_model,
                chromosome_model,
            )
            approved = self._approve_parameters(
                self._run_to_parameter_gate(
                    self._configured_state(tmpdir, assembly, reads)
                )
            )
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(approved)
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "blocked_missing_requirements")
            self.assertIn("chromosome", hic["reason"])
            self.assertNotIn("method_candidates", hic)

    def test_autohic_execution_failure_is_terminal_and_not_an_adoption_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(
                tmpdir,
                error_model,
                chromosome_model,
                fail=True,
            )
            approved = self._approve_parameters(
                self._run_to_parameter_gate(
                    self._configured_state(tmpdir, assembly, reads)
                )
            )
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(approved)
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertEqual(hic["status"], "failed")
            self.assertEqual(hic["failure_kind"], "execution")
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))
            self.assertEqual(
                hic["parameter_attempts"][0]["status"],
                "execution_failed",
            )
            self.assertIsNotNone(hic["parameter_attempts"][0]["execution_ref"])
            self.assertNotIn(
                "annotation_continues_on_pre_hic_assembly_fallback",
                hic.get("notes") or [],
            )
            self.assertIsNone(result.get("active_interrupt"))


if __name__ == "__main__":
    unittest.main()
