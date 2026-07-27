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
from app.execution.evidence import load_frozen_attempt
from app.execution.lifecycle import run_method_attempts
from app.review.base import ResumeReviewRequest
from app.review.persistence import load_review_request
from app.schemas.artifacts import (
    ArtifactMeta,
    ArtifactRef,
    ParameterAttempt,
    ParameterRecommendationCursor,
)
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.hic_scaffolding.builder import build_stage
from app.stages.hic_scaffolding.method_application.autohic import (
    LIFECYCLE,
    collect_autohic_artifacts,
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
from app.storage.artifacts import publish_artifact_record
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from app.workflow import next_stage_name
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
        self.assertEqual(
            registry.methods[AUTOHIC_METHOD].planner.required_parameters,
            [],
        )
        self.assertEqual(
            registry.methods[AUTOHIC_METHOD].planner.parameter_defaults,
            {},
        )
        self.assertFalse(any("yahs" in method.lower() for method in registry.methods))
        catalog = load_autohic_parameter_catalog()
        self.assertEqual(catalog.catalog_id, "autohic")
        self.assertEqual(catalog.method_id, AUTOHIC_METHOD)
        threads = catalog.parameters["threads"]
        self.assertIsNotNone(threads.layers.l0)
        self.assertIsNone(threads.layers.l2)
        self.assertEqual(threads.layers.l0.binding, "hic_threads")
        self.assertEqual(threads.layers.l0.materialized_default, 10)
        self.assertEqual(
            {
                name
                for name, specification in catalog.parameters.items()
                if specification.layers.l2 is not None
            },
            {
                "restriction_enzyme",
                "number_of_edit_rounds",
                "translocation_adjust",
                "inversion_adjust",
                "debris_adjust",
                "error_max_epoch",
                "error_min_len",
                "error_max_len",
                "error_filter_score",
                "error_filter_iou_score",
            },
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
        (autohic_root / "src" / "common").mkdir(parents=True, exist_ok=True)
        (autohic_root / "src" / "models" / "cfgs").mkdir(parents=True, exist_ok=True)
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
            "cfg = dict(line.strip().split('=', 1) for line in "
            "cfg_path.read_text().splitlines() if line.strip())\n"
            "if " + repr(fail) + ":\n"
            "    print('fake AutoHiC failed', file=sys.stderr)\n"
            "    raise SystemExit(17)\n"
            "assert pathlib.Path(cfg['REFERENCE_GENOME']).is_file()\n"
            "fastqs = sorted(path.name for path in "
            "(pathlib.Path(cfg['FASTQ_DIR']) / 'fastq').iterdir())\n"
            "assert fastqs == ['hic_001_R1.fastq.gz', 'hic_001_R2.fastq.gz']\n"
            "assert pathlib.Path(cfg['ERROR_PRETRAINED_MODEL']).is_file()\n"
            "assert pathlib.Path(cfg['CHR_PRETRAINED_MODEL']).is_file()\n"
            "top = pathlib.Path(cfg['RESULT_DIR']) / cfg['JOB_NAME']\n"
            "chromosome = top / 'autohic_results' / 'chromosome'\n"
            "chromosome.mkdir(parents=True, exist_ok=True)\n"
            "(top / 'contigs_autohic.fasta').write_text('>chr1\\nACGTACGT\\n')\n"
            "(chromosome / 'contigs.final.hic').write_text('hic\\n')\n"
            "(chromosome / 'contigs.final.assembly').write_text('assembly\\n')\n"
            "(top / 'result.html').write_text('<html>AutoHiC report</html>\\n')\n"
            "print('fake AutoHiC ok')\n",
            encoding="utf-8",
        )

        juicer_root = root / "juicer"
        (juicer_root / "CPU" / "common").mkdir(parents=True, exist_ok=True)
        (juicer_root / "misc").mkdir(parents=True, exist_ok=True)
        (juicer_root / "scripts" / "common").mkdir(parents=True, exist_ok=True)
        for relative in [
            "CPU/juicer.sh",
            "CPU/common/chimeric_blacklist.awk",
            "misc/generate_site_positions.py",
            "scripts/common/juicer_tools.jar",
        ]:
            (juicer_root / relative).write_text("stub\n", encoding="utf-8")

        three_d_dna_root = root / "3d-dna"
        (three_d_dna_root / "edit").mkdir(parents=True, exist_ok=True)
        (three_d_dna_root / "visualize").mkdir(parents=True, exist_ok=True)
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
            "    canary_paths: [autohic.py, src/common/run.sh, "
            "src/models/cfgs/error_model.py]\n"
            "    models:\n"
            f"      error: {error_model}\n"
            f"      chromosome: {chromosome_model}\n"
            "  juicer:\n"
            f"    install_root: {juicer_root}\n"
            "    canary_paths: [CPU/juicer.sh, "
            "CPU/common/chimeric_blacklist.awk, "
            "misc/generate_site_positions.py, "
            "scripts/common/juicer_tools.jar]\n"
            "  three_d_dna:\n"
            f"    install_root: {three_d_dna_root}\n"
            "    canary_paths: [run-asm-pipeline.sh, "
            "run-asm-pipeline-post-review.sh, "
            "edit/apply-edits-prep-for-next-round.sh, "
            "visualize/run-assembly-visualizer.sh]\n"
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
        parameters: dict | None = None,
    ) -> dict:
        model = _FakeModel(
            {
                "parameters": (
                    dict(parameters)
                    if parameters is not None
                    else {"restriction_enzyme": enzyme}
                ),
                "summary": "Library metadata supports this enzyme.",
            }
        )
        with patch(
            "app.llm.parameters.base.build_chat_model",
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

    def test_one_key_autohic_recommendation_always_blocks_approval(self):
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
                "app.llm.parameters.base.build_chat_model",
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
                request_dto["autohic_context"]["provided_restriction_enzyme"],
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

            snapshot_path = request_path.with_name("input_snapshot.json")
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["schema_version"], 3)
            self.assertEqual(
                snapshot["stage_context"]["provided_restriction_enzyme"],
                "HindIII",
            )
            self.assertNotIn("session_id", snapshot)
            self.assertNotIn("arbitrary_raw_metadata", json.dumps(snapshot))

    def test_initial_snapshot_keeps_registry_and_pre_hic_assembly_dependencies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads)
            paths = SessionPaths.from_run_dir(state["data_dir"])

            assembly_source = paths.work_path(
                "assembly_review", "pre_hic_assembly.fasta"
            )
            assembly_source.parent.mkdir(parents=True, exist_ok=True)
            assembly_source.write_bytes(assembly.read_bytes())
            published_assembly = publish_artifact_record(
                state["data_dir"],
                state["session_id"],
                ArtifactMeta.from_file(
                    assembly_source,
                    stage="assembly_review",
                    method_name="test.final_assembly",
                    artifact_type="assembly_review.final_assembly",
                ),
            )

            registry_source = paths.work_path("inventory", "registry.json")
            registry_source.parent.mkdir(parents=True, exist_ok=True)
            registry_source.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "assets": [
                            {
                                "asset_id": "s1:assembly_fasta_registered:adopted",
                                "asset_type": "assembly_fasta_registered",
                                "path": str(published_assembly.path),
                            },
                            *[
                                {
                                    "asset_id": f"s1:hic_reads_registered:{read.name}",
                                    "asset_type": "hic_reads_registered",
                                    "path": str(read),
                                    "metadata": {"library_protocol": "in_situ_hic"},
                                }
                                for read in reads
                            ],
                        ],
                    }
                ),
                encoding="utf-8",
            )
            published_registry = publish_artifact_record(
                state["data_dir"],
                state["session_id"],
                ArtifactMeta.from_file(
                    registry_source,
                    stage="inventory",
                    method_name="inventory.register_assets",
                    artifact_type="asset_registry",
                ),
            )
            state["asset_registry_ref"] = str(published_registry.path)

            result = self._run_to_parameter_gate(state)

            snapshot_path = (
                Path(result["data_dir"])
                / "artifacts"
                / "hic_scaffolding"
                / "parameter_recommendations"
                / "000"
                / "input_snapshot.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(
                snapshot["dependency_refs"]["stage_input_refs"],
                [
                    published_registry.ref.model_dump(mode="json"),
                    published_assembly.ref.model_dump(mode="json"),
                ],
            )

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
                "app.llm.parameters.base.build_chat_model",
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
                "app.llm.parameters.base.build_chat_model",
                return_value=_FakeModel(
                    {
                        "parameters": {"restriction_enzyme": "MboI"},
                        "summary": "Use the completed response.",
                    }
                ),
            ):
                first = recommend_autohic_parameters(resolved_state)

            with patch(
                "app.llm.parameters.base.build_chat_model",
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
            attempt = ParameterAttempt.model_validate(hic["parameter_attempts"][0])
            replay_attempt = ParameterAttempt.model_validate(
                replay_update["stage_outputs"]["hic_scaffolding"]["parameter_attempts"][
                    0
                ]
            )
            frozen_cursor = attempt.frozen_attempt_ref
            replay_cursor = replay_attempt.frozen_attempt_ref
            self.assertIsNotNone(frozen_cursor)
            self.assertEqual(replay_cursor, frozen_cursor)
            self.assertEqual(
                set(frozen_cursor.model_dump(mode="json")),
                {
                    "schema_version",
                    "artifact_id",
                    "path_ref",
                    "sha256",
                    "size_bytes",
                    "artifact_type",
                },
            )
            self.assertEqual(attempt.status, "frozen")
            self.assertIsNotNone(attempt.command_ref)
            self.assertIsNotNone(attempt.frozen_parameters_ref)
            self.assertNotIn("autohic_frozen_attempt_ref", hic)
            self.assertNotIn("autohic_execution_result", hic)
            frozen_attempt = load_frozen_attempt(
                frozen_state,
                frozen_cursor,
                expected_stage="hic_scaffolding",
                expected_method_name=AUTOHIC_METHOD,
            )

            command = json.loads(
                (
                    Path(frozen_state["data_dir"]) / attempt.command_ref.path_ref
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                command["frozen_parameters_ref"]["path_ref"],
                attempt.frozen_parameters_ref.path_ref,
            )
            self.assertEqual(
                str(frozen_attempt.script_path),
                str(
                    Path(frozen_state["data_dir"])
                    / "work"
                    / "hic_scaffolding"
                    / "autohic"
                    / "run_autohic.sh"
                ),
            )
            self.assertNotIn("frozen_attempt_ref", command)
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

    def test_ran_checkpoint_replays_collect_without_reexecuting_autohic(self):
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
                frozen_update = freeze_autohic_attempt(approved)
                frozen = {**approved, **frozen_update}
                ran_update = run_method_attempts(frozen, LIFECYCLE)
            ran = {**frozen, **ran_update}
            self.assertEqual(
                ran["stage_outputs"]["hic_scaffolding"]["status"], "autohic_ran"
            )

            first_collect = collect_autohic_artifacts(ran)
            first_candidate = first_collect["stage_outputs"]["hic_scaffolding"][
                "method_candidates"
            ][AUTOHIC_METHOD]["scaffold_fasta"]
            with patch(
                "app.execution.method_runner.LocalRunner.run",
                side_effect=AssertionError("ran recovery must not execute AutoHiC"),
            ):
                replay = build_stage().invoke(ran)

            replay_hic = replay["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(replay["supervision_state"], "blocked_human")
            self.assertEqual(replay["active_interrupt"]["gate_id"], "hic_scaffolding")
            self.assertEqual(
                replay_hic["method_candidates"][AUTOHIC_METHOD]["scaffold_fasta"],
                first_candidate,
            )
            self.assertEqual(replay_hic["parameter_attempts"][0]["status"], "executed")

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
                executed_attempt["execution_ref"]["artifact_type"],
                "hic_scaffolding.execution_result",
            )
            self.assertTrue(
                executed_attempt["execution_ref"]["path_ref"].startswith(
                    "artifacts/hic_scaffolding/attempts/"
                )
            )
            self.assertEqual(
                executed_attempt["frozen_attempt_ref"]["artifact_type"],
                "hic_scaffolding.frozen_attempt",
            )
            self.assertNotIn("autohic_frozen_attempt_ref", hic)
            self.assertNotIn("autohic_execution_result", hic)
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
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl")
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
                adopted["stage_outputs"]["hic_scaffolding"]["output_assembly_ref"],
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
                kept["stage_outputs"]["hic_scaffolding"]["output_assembly_ref"],
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

    def test_missing_python_command_after_approval_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads)
            del state["stage_outputs"]["hic_scaffolding"]["autohic_parameters"][
                "python_command"
            ]
            approved = self._approve_parameters(self._run_to_parameter_gate(state))

            result = freeze_autohic_attempt(approved)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "blocked_missing_requirements")
            self.assertEqual(
                hic["invalid_params"],
                [
                    {
                        "param": "autohic_parameters.python_command",
                        "value": "",
                        "reason": "must be non-empty",
                    }
                ],
            )

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
            self.assertEqual(hic["output_assembly_ref"], str(assembly))
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

    def test_empty_sparse_mapping_reaches_approval_and_uses_l1_enzyme(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(
                tmpdir, error_model, chromosome_model
            )
            blocked = self._run_to_parameter_gate(
                self._configured_state(tmpdir, assembly, reads),
                parameters={},
            )
            request = load_review_request(
                blocked["data_dir"], blocked["active_interrupt"]["request_ref"]
            )
            self.assertEqual(request["payload"]["parameters"], {})

            approved = self._approve_parameters(blocked)
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(approved)

            hic = result["stage_outputs"]["hic_scaffolding"]
            attempt = hic["parameter_attempts"][0]
            frozen = json.loads(
                (
                    Path(result["data_dir"])
                    / attempt["frozen_parameters_ref"]["path_ref"]
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(
                {
                    "l0_executor",
                    "l1_facts",
                    "l2_approved",
                    "effective_parameters",
                    "effective_source_by_parameter",
                    "applications",
                }.issubset(frozen)
            )
            self.assertEqual(frozen["l2_approved"], {})
            self.assertEqual(frozen["l1_facts"]["restriction_enzyme"], "HindIII")
            self.assertEqual(frozen["l0_executor"]["number_of_edit_rounds"], 2)
            self.assertEqual(
                frozen["effective_source_by_parameter"]["restriction_enzyme"],
                "l1",
            )
            self.assertEqual(
                frozen["effective_source_by_parameter"]["number_of_edit_rounds"],
                "l0",
            )
            self.assertEqual(frozen["parameters"]["restriction_enzyme"], "HindIII")

    def test_freeze_materializes_catalog_thread_default_when_run_config_omits_threads(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(
                tmpdir, error_model, chromosome_model
            )
            state = self._configured_state(tmpdir, assembly, reads)
            del state["stage_outputs"]["hic_scaffolding"]["autohic_parameters"][
                "threads"
            ]
            approved = self._approve_parameters(
                self._run_to_parameter_gate(state, parameters={})
            )

            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(approved)

            attempt = result["stage_outputs"]["hic_scaffolding"]["parameter_attempts"][
                0
            ]
            frozen = json.loads(
                (
                    Path(result["data_dir"])
                    / attempt["frozen_parameters_ref"]["path_ref"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(frozen["l0_executor"]["threads"], 10)
            self.assertEqual(frozen["parameters"]["threads"], 10)
            self.assertEqual(
                frozen["effective_source_by_parameter"]["threads"],
                "l0",
            )

    def test_freeze_uses_initial_enzyme_snapshot_after_current_state_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(
                tmpdir, error_model, chromosome_model
            )
            approved = self._approve_parameters(
                self._run_to_parameter_gate(
                    self._configured_state(tmpdir, assembly, reads),
                    parameters={},
                )
            )
            approved["stage_outputs"]["hic_scaffolding"]["autohic_parameters"][
                "restriction_enzyme"
            ] = "DpnII"

            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(approved)

            attempt = result["stage_outputs"]["hic_scaffolding"]["parameter_attempts"][
                0
            ]
            frozen = json.loads(
                (
                    Path(result["data_dir"])
                    / attempt["frozen_parameters_ref"]["path_ref"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(frozen["l1_facts"]["restriction_enzyme"], "HindIII")
            self.assertEqual(frozen["parameters"]["restriction_enzyme"], "HindIII")

    def test_freeze_ignores_legacy_adjustable_executor_overlay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(
                tmpdir, error_model, chromosome_model
            )
            approved = self._approve_parameters(
                self._run_to_parameter_gate(
                    self._configured_state(tmpdir, assembly, reads),
                    parameters={},
                )
            )
            approved["stage_outputs"]["hic_scaffolding"]["autohic_parameters"].update(
                {
                    "number_of_edit_rounds": 9,
                    "translocation_adjust": False,
                    "error_max_epoch": 19,
                }
            )

            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(approved)

            attempt = result["stage_outputs"]["hic_scaffolding"]["parameter_attempts"][
                0
            ]
            frozen = json.loads(
                (
                    Path(result["data_dir"])
                    / attempt["frozen_parameters_ref"]["path_ref"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(frozen["parameters"]["number_of_edit_rounds"], 2)
            self.assertTrue(frozen["parameters"]["translocation_adjust"])
            self.assertEqual(frozen["parameters"]["error_max_epoch"], 5)
            self.assertEqual(
                frozen["effective_source_by_parameter"]["number_of_edit_rounds"],
                "l0",
            )

    def test_execution_failure_retry_is_fresh_sparse_mapping_with_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads)
            state["stage_outputs"]["hic_scaffolding"]["parameter_retry_policy"] = {
                "retry_on_execution_failure": True,
                "max_execution_retries": 1,
                "max_review_retries": 1,
            }
            approved = self._approve_parameters(
                self._run_to_parameter_gate(state, enzyme="MboI")
            )
            envs_yaml = self._fake_autohic_environment(
                tmpdir, error_model, chromosome_model, fail=True
            )
            retry_model = _FakeModel(
                {
                    "parameters": {"number_of_edit_rounds": 3},
                    "summary": "Retry with one explicit override.",
                }
            )
            with (
                patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}),
                patch(
                    "app.llm.parameters.base.build_chat_model",
                    return_value=retry_model,
                ),
            ):
                retry_blocked = build_stage().invoke(approved)

            hic = retry_blocked["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(
                retry_blocked["active_interrupt"]["gate_id"],
                "parameter_approval",
            )
            self.assertEqual(hic["proposed_parameter_attempt_id"], 1)
            self.assertIsNotNone(hic["parameter_attempts"][0]["seal_hash"])
            retry_request = load_review_request(
                retry_blocked["data_dir"],
                retry_blocked["active_interrupt"]["request_ref"],
            )
            self.assertEqual(
                retry_request["payload"]["parameters"],
                {"number_of_edit_rounds": 3},
            )
            model_request = json.loads(retry_model.messages[1]["content"])
            retry_context = model_request["autohic_context"]["retry"]
            history = retry_context["attempt_history"]
            self.assertEqual(len(history), 1)
            self.assertEqual(
                history[0]["recommended_parameters"],
                {"restriction_enzyme": "MboI"},
            )
            self.assertEqual(
                history[0]["approved_parameters"],
                {"restriction_enzyme": "MboI"},
            )
            self.assertEqual(
                history[0]["effective_parameters"]["restriction_enzyme"],
                "MboI",
            )
            self.assertEqual(
                history[0]["effective_sources"]["restriction_enzyme"],
                "l2",
            )
            self.assertEqual(
                history[0]["status"],
                "execution_failed",
            )
            stderr_tail = history[0]["execution"]["stderr_tail"]
            self.assertIn("fake AutoHiC failed", stderr_tail)
            self.assertLessEqual(len(stderr_tail.encode("utf-8")), 16 * 1024)
            self.assertLessEqual(len(stderr_tail.splitlines()), 200)
            self.assertNotIn("stderr_ref", json.dumps(model_request))
            self.assertNotIn("path_ref", json.dumps(model_request))

            initial_manifest_ref = hic["parameter_attempts"][0][
                "recommendation_manifest_ref"
            ]
            initial_manifest = json.loads(
                (
                    Path(retry_blocked["data_dir"]) / initial_manifest_ref["path_ref"]
                ).read_text(encoding="utf-8")
            )
            retry_manifest_ref = hic["current_parameter_recommendation"]["manifest_ref"]
            retry_manifest = json.loads(
                (
                    Path(retry_blocked["data_dir"]) / retry_manifest_ref["path_ref"]
                ).read_text(encoding="utf-8")
            )
            initial_snapshot_ref = initial_manifest["refs"]["input_snapshot_ref"]
            retry_snapshot_ref = retry_manifest["refs"]["input_snapshot_ref"]
            initial_snapshot = json.loads(
                (
                    Path(retry_blocked["data_dir"]) / initial_snapshot_ref["path_ref"]
                ).read_text(encoding="utf-8")
            )
            retry_snapshot = json.loads(
                (
                    Path(retry_blocked["data_dir"]) / retry_snapshot_ref["path_ref"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(retry_snapshot["facts"], initial_snapshot["facts"])
            self.assertEqual(
                retry_snapshot["dependency_refs"]["fact_source_refs"],
                initial_snapshot["dependency_refs"]["fact_source_refs"],
            )
            self.assertEqual(
                retry_snapshot["dependency_refs"]["stage_input_refs"],
                initial_snapshot["dependency_refs"]["stage_input_refs"],
            )
            self.assertEqual(
                retry_snapshot["dependency_refs"]["initial_input_snapshot_ref"],
                initial_snapshot_ref,
            )
            self.assertEqual(
                retry_snapshot["dependency_refs"]["prior_attempt_manifest_refs"],
                [hic["parameter_attempts"][0]["attempt_manifest_ref"]],
            )

            retry_approved = self._approve_parameters(retry_blocked)
            self._fake_autohic_environment(
                tmpdir, error_model, chromosome_model, fail=False
            )
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(retry_approved)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(len(hic["parameter_attempts"]), 2)
            self.assertEqual(hic["parameter_attempts"][0]["status"], "execution_failed")
            frozen = json.loads(
                (
                    Path(result["data_dir"])
                    / hic["parameter_attempts"][1]["frozen_parameters_ref"]["path_ref"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(frozen["l2_approved"], {"number_of_edit_rounds": 3})
            self.assertEqual(frozen["parameters"]["restriction_enzyme"], "HindIII")
            self.assertEqual(frozen["parameters"]["number_of_edit_rounds"], 3)

    def test_review_can_request_fresh_parameter_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads)
            state["stage_outputs"]["hic_scaffolding"]["parameter_retry_policy"] = {
                "retry_on_execution_failure": False,
                "max_execution_retries": 0,
                "max_review_retries": 1,
            }
            envs_yaml = self._fake_autohic_environment(
                tmpdir, error_model, chromosome_model
            )
            approved = self._approve_parameters(self._run_to_parameter_gate(state))
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                reviewed = build_stage().invoke(approved)
            interrupt = reviewed["active_interrupt"]

            retry_requested = apply_hic_scaffolding_resume_decision(
                reviewed,
                ResumeReviewRequest(
                    session_id="s1",
                    stage="hic_scaffolding",
                    gate_id="hic_scaffolding",
                    decision="request_parameter_retry",
                    notes=["contact map needs another parameter attempt"],
                    review_request_id=interrupt["review_request_id"],
                    review_request_hash=interrupt["request_hash"],
                ),
            )
            retry_out = retry_requested["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(
                retry_out["status"], "retry_parameters_after_review_failure"
            )
            self.assertIsNotNone(retry_out["parameter_attempts"][0]["review_ref"])
            self.assertIsNotNone(retry_out["parameter_attempts"][0]["seal_hash"])

            retry_model = _FakeModel(
                {"parameters": {}, "summary": "Use ordinary defaults."}
            )
            with patch(
                "app.llm.parameters.base.build_chat_model",
                return_value=retry_model,
            ):
                retry_blocked = build_stage().invoke({**reviewed, **retry_requested})
            request = load_review_request(
                reviewed["data_dir"], retry_blocked["active_interrupt"]["request_ref"]
            )
            self.assertEqual(request["payload"]["parameters"], {})
            history = json.loads(retry_model.messages[1]["content"])["autohic_context"][
                "retry"
            ]["attempt_history"]
            self.assertEqual(history[0]["status"], "review_failed")
            self.assertEqual(
                history[0]["recommended_parameters"],
                {"restriction_enzyme": "MboI"},
            )
            self.assertEqual(
                history[0]["review"]["decision"],
                "request_parameter_retry",
            )

    def test_attempt_two_receives_ordered_execution_and_review_failure_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads)
            state["stage_outputs"]["hic_scaffolding"]["parameter_retry_policy"] = {
                "retry_on_execution_failure": True,
                "max_execution_retries": 1,
                "max_review_retries": 1,
            }
            attempt_zero = self._approve_parameters(
                self._run_to_parameter_gate(state, enzyme="MboI")
            )
            envs_yaml = self._fake_autohic_environment(
                tmpdir, error_model, chromosome_model, fail=True
            )
            attempt_one_model = _FakeModel(
                {
                    "parameters": {"number_of_edit_rounds": 3},
                    "summary": "Retry with fewer edit rounds.",
                }
            )
            with (
                patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}),
                patch(
                    "app.llm.parameters.base.build_chat_model",
                    return_value=attempt_one_model,
                ),
            ):
                attempt_one_blocked = build_stage().invoke(attempt_zero)

            attempt_one = self._approve_parameters(attempt_one_blocked)
            self._fake_autohic_environment(
                tmpdir, error_model, chromosome_model, fail=False
            )
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                attempt_one_review = build_stage().invoke(attempt_one)
            review_interrupt = attempt_one_review["active_interrupt"]
            attempt_two_requested = apply_hic_scaffolding_resume_decision(
                attempt_one_review,
                ResumeReviewRequest(
                    session_id="s1",
                    stage="hic_scaffolding",
                    gate_id="hic_scaffolding",
                    decision="request_parameter_retry",
                    notes=["try a different error length threshold"],
                    review_request_id=review_interrupt["review_request_id"],
                    review_request_hash=review_interrupt["request_hash"],
                ),
            )
            attempt_two_model = _FakeModel(
                {
                    "parameters": {"error_min_len": 200000},
                    "summary": "Use only a new error-length override.",
                }
            )
            with patch(
                "app.llm.parameters.base.build_chat_model",
                return_value=attempt_two_model,
            ):
                attempt_two_blocked = build_stage().invoke(
                    {**attempt_one_review, **attempt_two_requested}
                )

            history = json.loads(attempt_two_model.messages[1]["content"])[
                "autohic_context"
            ]["retry"]["attempt_history"]
            self.assertEqual([item["attempt_id"] for item in history], [0, 1])
            self.assertEqual(
                [item["status"] for item in history],
                ["execution_failed", "review_failed"],
            )
            self.assertEqual(
                history[0]["recommended_parameters"],
                {"restriction_enzyme": "MboI"},
            )
            self.assertEqual(
                history[1]["recommended_parameters"],
                {"number_of_edit_rounds": 3},
            )
            self.assertEqual(
                history[1]["review"]["decision"], "request_parameter_retry"
            )
            self.assertNotIn("stderr_tail", json.dumps(history))
            retry_request = load_review_request(
                attempt_two_blocked["data_dir"],
                attempt_two_blocked["active_interrupt"]["request_ref"],
            )
            self.assertEqual(
                retry_request["payload"]["parameters"], {"error_min_len": 200000}
            )
            retry_manifest_ref = attempt_two_blocked["stage_outputs"][
                "hic_scaffolding"
            ]["current_parameter_recommendation"]["manifest_ref"]
            retry_manifest = json.loads(
                (
                    Path(attempt_two_blocked["data_dir"])
                    / retry_manifest_ref["path_ref"]
                ).read_text(encoding="utf-8")
            )
            retry_snapshot_ref = retry_manifest["refs"]["input_snapshot_ref"]
            retry_snapshot = json.loads(
                (
                    Path(attempt_two_blocked["data_dir"])
                    / retry_snapshot_ref["path_ref"]
                ).read_text(encoding="utf-8")
            )
            attempts = attempt_two_blocked["stage_outputs"]["hic_scaffolding"][
                "parameter_attempts"
            ]
            self.assertEqual(
                retry_snapshot["dependency_refs"]["prior_attempt_manifest_refs"],
                [attempt["attempt_manifest_ref"] for attempt in attempts],
            )

            attempt_two = self._approve_parameters(attempt_two_blocked)
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                attempt_two_review = build_stage().invoke(attempt_two)

            hic = attempt_two_review["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(len(hic["parameter_attempts"]), 3)
            self.assertEqual(hic["parameter_attempts"][0]["status"], "execution_failed")
            self.assertEqual(hic["parameter_attempts"][1]["status"], "review_failed")
            self.assertEqual(
                hic["parameter_attempts"][2]["status"],
                "executed",
                json.dumps(hic, indent=2, sort_keys=True),
            )
            frozen = json.loads(
                (
                    Path(attempt_two_review["data_dir"])
                    / hic["parameter_attempts"][2]["frozen_parameters_ref"]["path_ref"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(frozen["l2_approved"], {"error_min_len": 200000})
            self.assertNotIn("number_of_edit_rounds", frozen["l2_approved"])
            self.assertNotIn("restriction_enzyme", frozen["l2_approved"])

    def test_missing_scaffold_uses_shared_collect_failure_boundary(self):
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

            with (
                patch.dict(
                    os.environ,
                    {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)},
                ),
                patch(
                    "app.stages.hic_scaffolding.method_application.support."
                    "publish_or_recover_work_artifact",
                    side_effect=FileNotFoundError("scaffold output disappeared"),
                ),
            ):
                result = build_stage().invoke(approved)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "failed")
            self.assertEqual(hic["failure_kind"], "collect")
            self.assertIn("scaffold output disappeared", hic["reason"])
            self.assertEqual(hic["parameter_attempts"][0]["status"], "executed")

            event_types = [
                event["event_type"]
                for event in Ledger(result["data_dir"]).read_events("s1")
            ]
            self.assertIn("attempt_failed", event_types)
            self.assertIn("stage_failed", event_types)
            self.assertNotIn("attempt_succeeded", event_types)


if __name__ == "__main__":
    unittest.main()
