"""T2T finishing stage contracts and conservative patching tests."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.control.lifecycle import ResumeValidationError, resume_review_state
from app.execution.evidence import load_frozen_attempt
from app.integrity import sha256_file
from app.review.base import ResumeReviewRequest
from app.review.persistence import load_review_request
from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.resource_ref import ResourceRef
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state
from app.stages.t2t.builder import build_stage
from app.stages.t2t.nodes import (
    _CANDIDATE_FIELDS,
    _candidate_rows,
    _materialize_t2t_patches,
    build_t2t_candidates,
    scan_read_telomeres,
)
from app.stages.t2t.parameters.catalog import load_t2t_parameter_catalog
from app.stages.t2t.parameters.execution import (
    T2TExecutionThresholds,
    T2TFrozenExecutionInputs,
)
from app.storage.artifacts import publish_artifact_record, read_artifact_records
from app.storage.asset_registry import load_asset_registry
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


class T2TDescriptorTests(unittest.TestCase):
    def test_registry_loads_t2t_minimap2_method(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
        method = registry.methods["t2t.alignment.minimap2"]
        self.assertEqual(method.stage, "t2t")
        self.assertEqual(method.package_ref, "minimap2")
        self.assertEqual(method.planner.required_parameters, [])
        self.assertEqual(method.planner.parameter_defaults, {})
        self.assertIn(method.method_name, registry.by_stage["t2t"])
        catalog = load_t2t_parameter_catalog()
        self.assertEqual(catalog.catalog_id, "minimap2")
        self.assertEqual(catalog.method_id, method.method_name)

    def test_run_config_leaves_catalog_owned_mode_unset(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": [
                    {"stage": "t2t", "allow_local_execution": True}
                ],
                "t2t": {},
            }
        )
        self.assertIsNone(config.t2t.mode)
        self.assertFalse(hasattr(config.t2t, "thresholds"))
        self.assertFalse(hasattr(config.t2t, "telomere_motif"))

    def test_run_config_rejects_legacy_telomere_motif(self):
        with self.assertRaises(ValueError):
            RunConfig.model_validate(
                {
                    "requested_stages": [
                        {"stage": "t2t", "allow_local_execution": True}
                    ],
                    "t2t": {"telomere_motif": "TTAGGG"},
                }
            )

    def test_run_config_rejects_legacy_threshold_overlay(self):
        with self.assertRaises(ValueError):
            RunConfig.model_validate(
                {
                    "requested_stages": [
                        {"stage": "t2t", "allow_local_execution": True}
                    ],
                    "t2t": {"thresholds": {"min_mapq": 30}},
                }
            )


class T2TStageTests(unittest.TestCase):
    def _publish_assembly_input(
        self, state: dict, source_path: Path, *, label: str = "assembly"
    ) -> Path:
        paths = SessionPaths.from_run_dir(state["data_dir"])
        staged = paths.workflow_step_work_path(
            "step-000-inventory", "inventory", f"{label}{source_path.suffix}"
        )
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(source_path.read_bytes())
        artifact = ArtifactMeta.from_file(
            staged,
            stage="inventory",
            method_name="test.input_registry",
            artifact_type="input.assembly_fasta",
        )
        return publish_artifact_record(
            state["data_dir"], state["session_id"], artifact
        ).path

    def _publish_long_read_input(
        self,
        state: dict,
        source_path: Path,
        *,
        label: str,
    ) -> Path:
        paths = SessionPaths.from_run_dir(state["data_dir"])
        staged = paths.workflow_step_work_path(
            "step-000-inventory", "inventory", f"{label}{source_path.suffix}"
        )
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(source_path.read_bytes())
        artifact = ArtifactMeta.from_file(
            staged,
            stage="inventory",
            method_name="test.input_registry",
            artifact_type="input.long_reads",
        )
        return publish_artifact_record(
            state["data_dir"], state["session_id"], artifact
        ).path

    def _publish_registry_ref(self, state: dict, content: str) -> dict:
        paths = SessionPaths.from_run_dir(state["data_dir"])
        registry = json.loads(content)
        ready_assets = []
        for asset in registry.get("assets", []):
            source_path = asset.get("path")
            if not source_path:
                ready_assets.append(asset)
                continue
            source = Path(source_path)
            ready_assets.append(
                {
                    **asset,
                    "checksum": sha256_file(source),
                    "checksum_status": "ready",
                    "size_bytes": source.stat().st_size,
                }
            )
        registry["assets"] = ready_assets
        source = paths.workflow_step_work_path(
            "step-000-inventory",
            "inventory",
            f"asset_registry_{os.urandom(4).hex()}.json",
        )
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(registry), encoding="utf-8")
        artifact = ArtifactMeta.from_file(
            source,
            stage="inventory",
            method_name="inventory.register_assets",
            artifact_type="asset_registry",
        )
        return publish_artifact_record(
            state["data_dir"], state["session_id"], artifact
        ).ref.model_dump(mode="json")

    def _invoke_with_parameter_approval(
        self,
        state: dict,
        *,
        recommended_parameters: dict | None = None,
    ) -> dict:
        """Run T2T while approving only its mandatory parameter gate."""

        graph = build_stage()
        current = state
        for _ in range(3):
            with (
                patch("app.llm.parameters.t2t.motif.build_chat_model") as motif_model,
                patch("app.llm.parameters.base.build_chat_model") as build_model,
            ):
                motif_model.return_value.invoke.return_value = SimpleNamespace(
                    content=json.dumps(
                        {
                            "status": "resolved",
                            "telomere_motif": "TTAGGG",
                            "summary": "Test species uses the canonical repeat.",
                        }
                    )
                )
                build_model.return_value.invoke.return_value = SimpleNamespace(
                    content=json.dumps(
                        {
                            "parameters": recommended_parameters or {},
                            "summary": "Use executor-owned defaults.",
                        }
                    )
                )
                result = graph.invoke(current)
            interrupt = result.get("active_interrupt") or {}
            if not (
                result.get("supervision_state") == "blocked_human"
                and interrupt.get("gate_id") == "parameter_approval"
            ):
                return result
            metadata_keys = (
                "attempt_id",
                "task",
                "previous_attempt_id",
                "recommendation_hash",
            )
            update = resume_review_state(
                result,
                ResumeReviewRequest(
                    session_id=result["session_id"],
                    stage="t2t",
                    gate_id="parameter_approval",
                    decision="approve_parameters",
                    review_request_id=interrupt["review_request_id"],
                    review_request_hash=interrupt["request_hash"],
                    metadata={key: interrupt[key] for key in metadata_keys},
                ),
            )
            current = {**result, **update}
        self.fail("T2T exceeded the bounded parameter-approval loop")

    def _state(self, tmpdir: str, assembly: Path, reads: Path | None = None):
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(tmpdir, "s1")),
            workflow_steps=[
                {
                    "step_id": "step-000-inventory",
                    "stage": "inventory",
                    "allow_local_execution": False,
                },
                {
                    "step_id": "step-001-t2t",
                    "stage": "t2t",
                    "allow_local_execution": True,
                },
                {
                    "step_id": "step-002-base_polish",
                    "stage": "base_polish",
                    "allow_local_execution": True,
                },
            ],
        )
        state["workflow_step_index"] = 1
        state["entered_workflow_step_index"] = 1
        state["current_stage"] = "t2t"
        published_assembly = self._publish_assembly_input(state, assembly)
        assets = []
        if reads:
            published_reads = self._publish_long_read_input(
                state, reads, label="clean_long_reads_0000"
            )
            assets.append(
                {
                    "asset_id": "s1:clean_long_reads:reads.fq",
                    "asset_type": "clean_long_reads",
                    "path": str(published_reads),
                    "platform": "ont",
                    "format": "fastq",
                }
            )
        state["asset_registry_ref"] = self._publish_registry_ref(
            state, json.dumps({"session_id": "s1", "assets": assets})
        )
        state["resource_refs"]["assembly"] = ResourceRef(
            resource_type="assembly",
            paths=[str(published_assembly)],
            source_stage="assembly_review",
        ).to_dict()
        state["shared_parameters"] = {
            "species_facts": {"scientific_name": "Homo sapiens", "taxon_id": 9606}
        }
        return state

    def _fake_minimap2(
        self,
        tmpdir: str,
        *,
        support_count: int = 2,
        paf_output: str | None = None,
    ) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "minimap2"
        paf_writer = (
            f"sys.stdout.write({paf_output!r})\n"
            if paf_output is not None
            else "for name, seq in records[:" + repr(support_count) + "]:\n"
            "    print(f'{name}\\t{len(seq)}\\t0\\t{target_len}\\t+\\t"
            "{target}\\t{target_len}\\t0\\t{target_len}\\t{target_len}\\t"
            "{target_len}\\t60')\n"
        )
        script.write_text(
            (
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "assembly = sys.argv[-2]\n"
                "reads = sys.argv[-1]\n"
                "target = None\n"
                "target_seq = []\n"
                "for line in open(assembly):\n"
                "    if line.startswith('>'):\n"
                "        if target is None:\n"
                "            target = line[1:].strip().split()[0]\n"
                "        continue\n"
                "    target_seq.append(line.strip())\n"
                "target_len = len(''.join(target_seq))\n"
                "records = []\n"
                "name = None\n"
                "sequence = []\n"
                "for line in open(reads):\n"
                "    if line.startswith('>'):\n"
                "        if name is not None:\n"
                "            records.append((name, ''.join(sequence)))\n"
                "        name = line[1:].strip().split()[0]\n"
                "        sequence = []\n"
                "    else:\n"
                "        sequence.append(line.strip())\n"
                "if name is not None:\n"
                "    records.append((name, ''.join(sequence)))\n"
            )
            + paf_writer,
            encoding="utf-8",
        )
        script.chmod(0o755)
        return bin_dir

    def _write_inputs(self, tmpdir: str) -> tuple[Path, Path]:
        assembly = Path(tmpdir) / "final.fa"
        core = "TTAGGG" * 4 + "A" * 10_050
        assembly.write_text(f">chr1\n{core}\n", encoding="utf-8")
        reads = Path(tmpdir) / "reads.fq"
        reads.write_text(
            "".join(
                f"@r{i}\n{core}{'TTAGGG' * 4}\n+\n{'I' * (len(core) + 24)}\n"
                for i in range(1, 3)
            ),
            encoding="utf-8",
        )
        return assembly, reads

    def test_auto_patch_preserves_input_and_hands_patched_assembly_to_base_polish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            original = assembly.read_text(encoding="utf-8")
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }

            blocked = self._invoke_with_parameter_approval(state)

            self.assertEqual(blocked["current_stage"], "t2t")
            self.assertEqual(blocked["supervision_state"], "blocked_human")
            t2t = blocked["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "blocked_human")
            patched = Path(t2t["patched_assembly_ref"])
            self.assertTrue(patched.exists())
            self.assertEqual(assembly.read_text(encoding="utf-8"), original)
            self.assertIn("TTAGGG" * 4, patched.read_text(encoding="utf-8"))
            self.assertNotEqual(t2t["output_assembly_ref"], str(patched))
            self.assertEqual(
                t2t["alignment_paf_artifact_ref"]["artifact_type"],
                "t2t.read_alignment_paf",
            )
            with Path(t2t["telomere_extension_candidates_ref"]).open(
                encoding="utf-8"
            ) as handle:
                candidate_rows = list(csv.DictReader(handle, delimiter="\t"))
            selected_candidate_ids = [
                row["candidate_id"]
                for row in candidate_rows
                if row["decision"] == "auto_patch"
            ]
            self.assertTrue(selected_candidate_ids)
            self.assertTrue(candidate_rows[0]["frozen_attempt_ref"])
            self.assertTrue(candidate_rows[0]["execution_ref"])
            self.assertTrue(candidate_rows[0]["alignment_paf_ref"])
            event_types = [
                event["event_type"]
                for event in Ledger(blocked["data_dir"]).read_events("s1")
            ]
            self.assertIn("attempt_succeeded", event_types)

            result = resume_review_state(
                blocked,
                ResumeReviewRequest(
                    session_id="s1",
                    stage="t2t",
                    gate_id="t2t_review",
                    decision="accept_selected_candidates",
                    candidate_ids=selected_candidate_ids,
                    review_request_id=blocked["active_interrupt"][
                        "review_request_id"
                    ],
                    review_request_hash=blocked["active_interrupt"]["request_hash"],
                ),
            )

            self.assertEqual(result["current_stage"], "base_polish")
            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "completed")
            self.assertEqual(t2t["output_assembly_ref"], str(patched))
            self.assertEqual(
                result["resource_refs"]["assembly"]["paths"], [str(patched)]
            )

    def test_execution_is_plan_only_without_local_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            expected_assembly = state["resource_refs"]["assembly"]["paths"][0]
            result = self._invoke_with_parameter_approval(state)

            self.assertEqual(result["current_stage"], "base_polish")
            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "skipped")
            self.assertEqual(t2t["output_assembly_ref"], expected_assembly)
            self.assertTrue(Path(t2t["t2t_finishing_report_ref"]).exists())

    def test_hic_final_assembly_has_priority_over_assembly_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            review_assembly, reads = self._write_inputs(tmpdir)
            hic_assembly = Path(tmpdir) / "hic_final.fa"
            hic_assembly.write_text(
                ">chrH\n" + "TTAGGG" * 4 + "A" * 10_050 + "\n", encoding="utf-8"
            )
            state = self._state(tmpdir, review_assembly, reads)
            published_hic = self._publish_assembly_input(
                state, hic_assembly, label="hic_assembly"
            )
            state["stage_outputs"]["hic_scaffolding"] = {
                "output_assembly_ref": str(published_hic)
            }
            state["resource_refs"]["assembly"] = ResourceRef(
                resource_type="assembly",
                paths=[str(published_hic)],
                source_stage="hic_scaffolding",
            ).to_dict()

            result = self._invoke_with_parameter_approval(state)

            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["source_stage"], "hic_scaffolding")
            self.assertEqual(t2t["input_assembly_ref"], str(published_hic))

    def test_run_config_assembly_cannot_bypass_current_adopted_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            bypass = Path(tmpdir) / "bypass.fa"
            bypass.write_text(">bypass\n" + "A" * 10_000 + "\n", encoding="utf-8")
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "assembly_fasta_ref": str(bypass),
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "t2t")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn(
                "must match the current adopted assembly",
                result["last_error"],
            )

    def test_invalid_assembly_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "not_fasta.txt"
            assembly.write_text("not a fasta\n", encoding="utf-8")
            result = build_stage().invoke(self._state(tmpdir, assembly))

            self.assertEqual(result["current_stage"], "t2t")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("valid FASTA", result["last_error"])

    def test_single_read_evidence_patches_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir, support_count=1))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }
            blocked = self._invoke_with_parameter_approval(state)

            t2t = blocked["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "blocked_human")
            self.assertTrue(Path(t2t["patched_assembly_ref"]).exists())
            with Path(t2t["telomere_extension_candidates_ref"]).open(
                encoding="utf-8"
            ) as handle:
                candidate_rows = list(csv.DictReader(handle, delimiter="\t"))
            selected_candidate_ids = [
                row["candidate_id"]
                for row in candidate_rows
                if row["decision"] == "auto_patch"
            ]
            self.assertEqual(selected_candidate_ids, ["chr1:right"])
            self.assertEqual(candidate_rows[0]["supporting_reads"].count(","), 0)

            resumed = resume_review_state(
                blocked,
                ResumeReviewRequest(
                    session_id="s1",
                    stage="t2t",
                    gate_id="t2t_review",
                    decision="accept_selected_candidates",
                    candidate_ids=selected_candidate_ids,
                    review_request_id=blocked["active_interrupt"][
                        "review_request_id"
                    ],
                    review_request_hash=blocked["active_interrupt"]["request_hash"],
                ),
            )

            t2t = resumed["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "completed")
            self.assertEqual(
                resumed["resource_refs"]["assembly"]["paths"],
                [t2t["patched_assembly_ref"]],
            )

    def test_approved_two_read_threshold_still_blocks_single_read_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir, support_count=1))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }
            result = self._invoke_with_parameter_approval(
                state,
                recommended_parameters={"min_supporting_reads": 2},
            )

            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "completed_with_warnings")
            self.assertNotIn("patched_assembly_ref", t2t)
            candidate_rows = Path(t2t["telomere_extension_candidates_ref"]).read_text(
                encoding="utf-8"
            )
            self.assertIn("insufficient_or_conflicting_read_support", candidate_rows)

    def test_build_candidates_reentry_preserves_existing_candidate_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "out"
            out_dir.mkdir()
            candidates = out_dir / "telomere_extension_candidates.tsv"
            original = (
                "candidate_id\ttarget\ttarget_end\tread_id\tread_end\tstrand\tmapq\toverlap_bp\t"
                "target_distance_bp\textension_length\tquery_start\tquery_end\tsupporting_reads\tdecision\treason\n"
                "chr1:right\tchr1\tright\tr1\tright\t+\t60\t1000\t0\t24\t0\t1000\tr1\tproposed\tapproved_read_support_threshold\n"
            )
            candidates.write_text(original, encoding="utf-8")
            state = initial_state(
                session_id="s1",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(v2_run_dir(tmpdir, "s1")),
                workflow_steps=[
                    {
                        "step_id": "step-000-inventory",
                        "stage": "inventory",
                        "allow_local_execution": False,
                    },
                    {
                        "step_id": "step-001-t2t",
                        "stage": "t2t",
                        "allow_local_execution": True,
                    },
                ],
            )
            state["workflow_step_index"] = 1
            state["entered_workflow_step_index"] = 1
            state["current_stage"] = "t2t"
            state["stage_outputs"] = {
                "t2t": {
                    "status": "candidates_built",
                    "telomere_extension_candidates_ref": str(candidates),
                    "candidate_count": 1,
                }
            }

            update = build_t2t_candidates(state)

            self.assertEqual(update, {})
            self.assertEqual(candidates.read_text(encoding="utf-8"), original)

    def test_collect_replay_recovers_published_paf_after_work_file_is_moved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }

            completed = self._invoke_with_parameter_approval(state)
            original = completed["stage_outputs"]["t2t"]
            paf_work_path = Path(original["work_dir"]) / "long_reads_to_assembly.paf"
            self.assertFalse(paf_work_path.exists())

            replay_t2t = dict(original)
            replay_t2t["status"] = "alignment_ran"
            for key in (
                "alignment_paf_ref",
                "alignment_paf_artifact_ref",
                "telomere_extension_candidates_ref",
                "telomere_extension_candidates_artifact_ref",
                "candidate_count",
                "selected_patch_count",
                "proposed_candidate_count",
            ):
                replay_t2t.pop(key, None)
            replay_state = {
                **completed,
                "stage_outputs": {
                    **completed["stage_outputs"],
                    "t2t": replay_t2t,
                },
            }

            replay = build_t2t_candidates(replay_state)
            replay_output = replay["stage_outputs"]["t2t"]
            self.assertEqual(
                replay_output["alignment_paf_artifact_ref"],
                original["alignment_paf_artifact_ref"],
            )
            self.assertEqual(
                replay_output["telomere_extension_candidates_artifact_ref"],
                original["telomere_extension_candidates_artifact_ref"],
            )

    def test_malformed_paf_fails_collect_without_recording_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "env": {
                    "PATH": str(
                        self._fake_minimap2(tmpdir, paf_output="malformed paf row\\n")
                    )
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }

            result = self._invoke_with_parameter_approval(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("malformed PAF line 1", result["last_error"])
            event_types = [
                event["event_type"]
                for event in Ledger(result["data_dir"]).read_events("s1")
            ]
            self.assertNotIn("attempt_succeeded", event_types)

    def test_malformed_clean_registry_member_blocks_without_falling_back_to_raw(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            raw_reads = Path(tmpdir) / "raw.fq"
            raw_reads.write_text(reads.read_text(encoding="utf-8"), encoding="utf-8")
            state = self._state(tmpdir, assembly)
            state["asset_registry_ref"] = self._publish_registry_ref(
                state,
                json.dumps(
                    {
                        "session_id": "s1",
                        "assets": [
                            {
                                "asset_id": "clean-valid",
                                "asset_type": "clean_long_reads",
                                "path": str(reads),
                                "platform": "ont",
                                "format": "fastq",
                            },
                            {
                                "asset_id": "clean-missing-path",
                                "asset_type": "clean_long_reads",
                                "platform": "ont",
                                "format": "fastq",
                            },
                            {
                                "asset_id": "raw-valid",
                                "asset_type": "raw_long_reads",
                                "path": str(raw_reads),
                                "platform": "ont",
                                "format": "fastq",
                            },
                        ],
                    }
                ),
            )
            state["stage_outputs"]["t2t"] = {"execution_mode": "local"}

            with patch(
                "app.llm.parameters.base.build_chat_model",
                side_effect=AssertionError(
                    "malformed registry input must fail before the model"
                ),
            ):
                result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("clean-missing-path", result["last_error"])

    def test_manual_review_blocks_only_when_proposed_candidates_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }

            result = self._invoke_with_parameter_approval(state)

            self.assertEqual(result["current_stage"], "t2t")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(result["stage_outputs"]["t2t"]["status"], "blocked_human")
            self.assertEqual(result["active_interrupt"]["stage"], "t2t")
            self.assertEqual(result["active_interrupt"]["gate_id"], "t2t_review")
            request_payload = load_review_request(
                result["data_dir"], result["active_interrupt"]["request_ref"]
            )["payload"]
            self.assertEqual(
                request_payload["decisions_allowed"],
                [
                    "accept_selected_candidates",
                    "continue_without_patch",
                    "stop_pipeline",
                ],
            )
            self.assertEqual(
                [
                    candidate["candidate_id"]
                    for candidate in request_payload["candidates"]
                ],
                ["chr1:right"],
            )

    def test_manual_review_without_proposed_candidates_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir, support_count=1))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }

            result = self._invoke_with_parameter_approval(
                state,
                recommended_parameters={"min_supporting_reads": 2},
            )

            self.assertEqual(result["current_stage"], "base_polish")
            self.assertNotEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(
                result["stage_outputs"]["t2t"]["status"], "completed_with_warnings"
            )

    def test_manual_review_resume_accepts_explicit_selected_candidate_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }
            blocked = self._invoke_with_parameter_approval(state)
            request = ResumeReviewRequest(
                session_id="s1",
                stage="t2t",
                gate_id="t2t_review",
                decision="accept_selected_candidates",
                candidate_ids=["chr1:right", "chr1:right"],
                review_request_id=blocked["active_interrupt"]["review_request_id"],
                review_request_hash=blocked["active_interrupt"]["request_hash"],
            )

            resumed = resume_review_state(blocked, request)

            self.assertEqual(resumed["current_stage"], "base_polish")
            self.assertEqual(resumed["supervision_state"], "running_stage")
            self.assertIsNone(resumed["active_interrupt"])
            t2t = resumed["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "completed")
            self.assertEqual(t2t["selected_candidate_ids"], ["chr1:right"])
            self.assertEqual(t2t["selected_patch_count"], 1)
            self.assertTrue(Path(t2t["patched_assembly_ref"]).exists())
            self.assertEqual(t2t["output_assembly_ref"], t2t["patched_assembly_ref"])
            self.assertTrue(Path(t2t["patched_telomere_scan_ref"]).exists())
            self.assertEqual(
                resumed["resource_refs"]["assembly"]["paths"],
                [t2t["patched_assembly_ref"]],
            )

    def test_manual_review_resume_continue_without_patch_uses_input_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            expected_assembly = state["resource_refs"]["assembly"]["paths"][0]
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }
            blocked = self._invoke_with_parameter_approval(state)
            request = ResumeReviewRequest(
                session_id="s1",
                stage="t2t",
                gate_id="t2t_review",
                decision="continue_without_patch",
                candidate_ids=["chr1:right"],
                review_request_id=blocked["active_interrupt"]["review_request_id"],
                review_request_hash=blocked["active_interrupt"]["request_hash"],
            )

            resumed = resume_review_state(blocked, request)

            self.assertEqual(resumed["current_stage"], "base_polish")
            t2t = resumed["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "completed_with_warnings")
            self.assertEqual(t2t["output_assembly_ref"], expected_assembly)
            self.assertIsNone(t2t["patched_assembly_ref"])
            self.assertEqual(t2t["selected_candidate_ids"], [])
            self.assertEqual(t2t["selected_patch_count"], 0)
            self.assertEqual(
                resumed.get("resource_refs", blocked["resource_refs"])["assembly"][
                    "paths"
                ],
                [expected_assembly],
            )

    def test_manual_review_resume_rejects_changed_candidate_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }
            blocked = self._invoke_with_parameter_approval(state)
            candidates = Path(
                blocked["stage_outputs"]["t2t"][
                    "telomere_extension_candidates_ref"
                ]
            )
            candidates.write_text(
                candidates.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            request = ResumeReviewRequest(
                session_id="s1",
                stage="t2t",
                gate_id="t2t_review",
                decision="accept_selected_candidates",
                candidate_ids=["chr1:right"],
                review_request_id=blocked["active_interrupt"]["review_request_id"],
                review_request_hash=blocked["active_interrupt"]["request_hash"],
            )

            with self.assertRaisesRegex(
                ResumeValidationError,
                "review evidence changed before adoption",
            ):
                resume_review_state(blocked, request)

            continue_update = resume_review_state(
                blocked,
                ResumeReviewRequest(
                    session_id="s1",
                    stage="t2t",
                    gate_id="t2t_review",
                    decision="continue_without_patch",
                    review_request_id=blocked["active_interrupt"][
                        "review_request_id"
                    ],
                    review_request_hash=blocked["active_interrupt"]["request_hash"],
                ),
            )
            resumed = {**blocked, **continue_update}
            t2t = resumed["stage_outputs"]["t2t"]
            self.assertEqual(t2t["output_assembly_ref"], t2t["input_assembly_ref"])
            self.assertEqual(
                resumed["resource_refs"]["assembly"]["paths"],
                [t2t["input_assembly_ref"]],
            )

    def test_review_resume_refuses_to_overwrite_a_newer_assembly_resource(self):
        for decision in ("accept_selected_candidates", "continue_without_patch"):
            with (
                self.subTest(decision=decision),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                assembly, reads = self._write_inputs(tmpdir)
                state = self._state(tmpdir, assembly, reads)
                state["stage_outputs"]["t2t"] = {
                    "execution_mode": "local",
                    "mode": "manual_review",
                    "env": {
                        "PATH": str(self._fake_minimap2(tmpdir))
                        + os.pathsep
                        + os.environ["PATH"]
                    },
                }
                blocked = self._invoke_with_parameter_approval(state)
                stale = Path(tmpdir) / "newer.fa"
                stale.write_text(">newer\nACGT\n", encoding="utf-8")
                published_stale = self._publish_assembly_input(
                    blocked,
                    stale,
                    label=f"newer_{decision}",
                )
                blocked["resource_refs"]["assembly"] = ResourceRef(
                    resource_type="assembly",
                    paths=[str(published_stale)],
                    source_stage="external_update",
                ).to_dict()
                request = ResumeReviewRequest(
                    session_id="s1",
                    stage="t2t",
                    gate_id="t2t_review",
                    decision=decision,
                    candidate_ids=(
                        ["chr1:right"]
                        if decision == "accept_selected_candidates"
                        else []
                    ),
                    review_request_id=blocked["active_interrupt"][
                        "review_request_id"
                    ],
                    review_request_hash=blocked["active_interrupt"]["request_hash"],
                )
                artifacts_before = read_artifact_records(blocked["data_dir"], "s1")
                events_before = Ledger(blocked["data_dir"]).read_events("s1")

                update = resume_review_state(blocked, request)

                self.assertEqual(update["current_stage"], "t2t")
                self.assertEqual(update["supervision_state"], "failed_terminal")
                self.assertIn("changed since input resolution", update["last_error"])
                self.assertNotIn("resource_refs", update)
                self.assertEqual(
                    blocked["resource_refs"]["assembly"]["paths"],
                    [str(published_stale)],
                )
                self.assertEqual(
                    read_artifact_records(blocked["data_dir"], "s1"),
                    artifacts_before,
                )
                self.assertEqual(
                    Ledger(blocked["data_dir"]).read_events("s1"),
                    events_before,
                )

    def test_review_resume_rejects_non_human_candidate_adoption(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }
            blocked = self._invoke_with_parameter_approval(state)
            artifacts_before = read_artifact_records(blocked["data_dir"], "s1")
            events_before = Ledger(blocked["data_dir"]).read_events("s1")

            with self.assertRaisesRegex(
                ResumeValidationError,
                "requires a human review decision",
            ):
                resume_review_state(
                    blocked,
                    ResumeReviewRequest(
                        session_id="s1",
                        stage="t2t",
                        gate_id="t2t_review",
                        decision="accept_selected_candidates",
                        candidate_ids=["chr1:right"],
                        review_request_id=blocked["active_interrupt"][
                            "review_request_id"
                        ],
                        review_request_hash=blocked["active_interrupt"][
                            "request_hash"
                        ],
                        decided_by="llm_provisional",
                        rationale_artifact_ref="llm:t2t-adoption",
                    ),
                )

            self.assertEqual(
                read_artifact_records(blocked["data_dir"], "s1"),
                artifacts_before,
            )
            self.assertEqual(
                Ledger(blocked["data_dir"]).read_events("s1"),
                events_before,
            )

    def test_manual_review_resume_stop_pipeline_does_not_enter_downstream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }
            blocked = self._invoke_with_parameter_approval(state)
            request = ResumeReviewRequest(
                session_id="s1",
                stage="t2t",
                gate_id="t2t_review",
                decision="stop_pipeline",
                review_request_id=blocked["active_interrupt"]["review_request_id"],
                review_request_hash=blocked["active_interrupt"]["request_hash"],
            )

            stopped = resume_review_state(blocked, request)

            self.assertEqual(stopped["current_stage"], "t2t")
            self.assertEqual(stopped["supervision_state"], "failed_terminal")
            self.assertIsNone(stopped["active_interrupt"])

    def test_propose_only_with_candidates_is_non_blocking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "propose_only",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }

            result = self._invoke_with_parameter_approval(state)

            self.assertEqual(result["current_stage"], "base_polish")
            self.assertNotEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(
                result["stage_outputs"]["t2t"]["status"], "completed_with_warnings"
            )
            self.assertIsNone(result.get("active_interrupt"))

    def test_alignment_uses_filtered_telomeric_reads_fasta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            with reads.open("a", encoding="utf-8") as handle:
                handle.write(
                    "@not_telomeric\n" + "C" * 2000 + "\n+\n" + "I" * 2000 + "\n"
                )
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }

            result = self._invoke_with_parameter_approval(state)

            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["read_count"], 3)
            self.assertEqual(t2t["telomeric_read_count"], 2)
            attempt = load_frozen_attempt(
                result,
                t2t["parameter_attempts"][0]["frozen_attempt_ref"],
                expected_stage="t2t",
                expected_method_name="t2t.alignment.minimap2",
            )
            self.assertEqual(
                attempt.metadata["reads_refs"],
                t2t["telomeric_reads_fasta_refs"],
            )
            for reads_ref in t2t["telomeric_reads_fasta_refs"].values():
                self.assertNotIn(
                    "not_telomeric",
                    Path(reads_ref).read_text(encoding="utf-8"),
                )

    def test_mixed_platform_reads_execute_every_minimap2_preset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly)
            platforms = ("ont", "pacbio_hifi", "clr", "cycloneseq")
            assets = []
            for index, platform in enumerate(platforms):
                source = Path(tmpdir) / f"{platform}.fq"
                source.write_bytes(reads.read_bytes())
                published = self._publish_long_read_input(
                    state,
                    source,
                    label=f"clean_long_reads_{index:04d}",
                )
                assets.append(
                    {
                        "asset_id": f"s1:clean_long_reads:{platform}",
                        "asset_type": "clean_long_reads",
                        "path": str(published),
                        "platform": platform,
                        "format": "fastq",
                    }
                )
            state["asset_registry_ref"] = self._publish_registry_ref(
                state,
                json.dumps({"session_id": "s1", "assets": assets}),
            )
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }

            result = self._invoke_with_parameter_approval(state)

            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(
                [item["platform"] for item in t2t["long_read_inputs"]],
                list(platforms),
            )
            self.assertEqual(
                set(t2t["telomeric_reads_fasta_refs"]),
                {"map-ont", "map-hifi", "map-pb"},
            )
            self.assertEqual(t2t["read_count"], 8)
            self.assertEqual(t2t["telomeric_read_count"], 8)
            attempt = load_frozen_attempt(
                result,
                t2t["parameter_attempts"][0]["frozen_attempt_ref"],
                expected_stage="t2t",
                expected_method_name="t2t.alignment.minimap2",
            )
            self.assertEqual(
                set(attempt.metadata["reads_refs"]),
                {"map-ont", "map-hifi", "map-pb"},
            )
            script = Path(t2t["work_dir"]) / "run_minimap2_t2t.sh"
            script_text = script.read_text(encoding="utf-8")
            for preset in ("map-ont", "map-hifi", "map-pb"):
                self.assertEqual(script_text.count(f"minimap2 -x {preset} "), 1)

    def test_scan_read_telomeres_streams_outputs_without_bulk_tsv_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            registered_read = load_asset_registry(state).assets[0]
            read_input = {
                "member_id": registered_read.asset_id,
                "source_asset_id": registered_read.asset_id,
                "path": str(registered_read.path),
                "platform": registered_read.platform,
            }
            paths = SessionPaths.from_run_dir(state["data_dir"])
            work_dir = paths.workflow_step_work_path(
                "step-001-t2t", "t2t", "t2t_finishing"
            )
            work_dir.mkdir(parents=True)
            state["stage_outputs"]["t2t"] = {
                "status": "reads_prepared",
                "long_read_inputs": [read_input],
                "work_dir": str(work_dir),
            }
            frozen_inputs = T2TFrozenExecutionInputs(
                resolved_telomere_motif="TTAGGG",
                motif_source="provided_species_fact",
                mode="auto_patch",
                input_assembly_ref=str(assembly),
                long_read_inputs=(read_input,),
                chromosome_list_ref=None,
                work_dir=str(work_dir),
                execution_enabled=True,
                thresholds=T2TExecutionThresholds.model_validate(
                    {
                        "min_motif_copies": 4,
                        "terminal_window_bp": 10_000,
                        "min_mapq": 20,
                        "max_target_terminal_distance_bp": 1_000,
                        "min_overlap_bp": 500,
                        "min_supporting_reads": 2,
                    }
                ),
            )

            with (
                patch(
                    "app.stages.t2t.nodes.load_t2t_frozen_execution_inputs",
                    return_value=frozen_inputs,
                ),
                patch(
                    "app.stages.t2t.nodes._write_tsv",
                    side_effect=AssertionError("bulk TSV write called"),
                ),
            ):
                update = scan_read_telomeres(state)

            t2t = update["stage_outputs"]["t2t"]
            self.assertEqual(t2t["read_count"], 2)
            self.assertEqual(t2t["telomeric_read_count"], 2)
            self.assertTrue(Path(t2t["reads_telomere_scan_ref"]).exists())
            self.assertTrue(Path(t2t["telomeric_reads_ref"]).exists())
            self.assertEqual(
                set(t2t["telomeric_reads_fasta_refs"]),
                {"map-ont"},
            )
            self.assertTrue(
                Path(t2t["telomeric_reads_fasta_refs"]["map-ont"]).exists()
            )

    def test_all_clean_read_files_are_scanned_before_raw_and_ids_are_namespaced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, _ = self._write_inputs(tmpdir)
            clean_a = Path(tmpdir) / "clean_a.fq"
            clean_b = Path(tmpdir) / "clean_b.fq"
            raw = Path(tmpdir) / "raw.fq"
            sequence = "A" * 700 + "TTAGGG" * 4
            for path in (clean_a, clean_b, raw):
                path.write_text(
                    f"@duplicate\n{sequence}\n+\n{'I' * len(sequence)}\n",
                    encoding="utf-8",
                )
            state = self._state(tmpdir, assembly)
            published_clean_a = self._publish_long_read_input(
                state, clean_a, label="clean_long_reads_0000"
            )
            published_clean_b = self._publish_long_read_input(
                state, clean_b, label="clean_long_reads_0001"
            )
            published_raw = self._publish_long_read_input(
                state, raw, label="raw_long_reads_0000"
            )
            state["asset_registry_ref"] = self._publish_registry_ref(
                state,
                json.dumps(
                    {
                        "session_id": "s1",
                        "assets": [
                            {
                                "asset_id": "clean-a",
                                "asset_type": "clean_long_reads",
                                "path": str(published_clean_a),
                                "platform": "ont",
                                "format": "fastq",
                            },
                            {
                                "asset_id": "raw-a",
                                "asset_type": "raw_long_reads",
                                "path": str(published_raw),
                                "platform": "ont",
                                "format": "fastq",
                            },
                            {
                                "asset_id": "clean-b",
                                "asset_type": "clean_long_reads",
                                "path": str(published_clean_b),
                                "platform": "ont",
                                "format": "fastq",
                            },
                        ],
                    }
                ),
            )
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "env": {
                    "PATH": str(self._fake_minimap2(tmpdir))
                    + os.pathsep
                    + os.environ["PATH"]
                },
            }

            result = self._invoke_with_parameter_approval(state)
            t2t = result["stage_outputs"]["t2t"]

            self.assertEqual(
                t2t["long_read_refs"],
                [str(published_clean_a), str(published_clean_b)],
            )
            self.assertEqual(t2t["read_count"], 2)
            with Path(t2t["reads_telomere_scan_ref"]).open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(
                [row["read_id"] for row in rows],
                ["clean-a::duplicate", "clean-b::duplicate"],
            )
            self.assertEqual(
                [row["source_asset_id"] for row in rows], ["clean-a", "clean-b"]
            )

    def test_no_read_path_publishes_empty_tables_without_invoking_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, _ = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly)
            state["stage_outputs"]["t2t"] = {"execution_mode": "local"}

            with patch(
                "app.llm.parameters.base.build_chat_model",
                side_effect=AssertionError("no-read T2T must not invoke the model"),
            ):
                result = build_stage().invoke(state)

            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "skipped_no_long_reads")
            for key in (
                "reads_telomere_scan_ref",
                "telomeric_reads_ref",
                "telomere_extension_candidates_ref",
            ):
                self.assertTrue(Path(t2t[key]).is_file())
                self.assertIn("/artifacts/steps/step-001-t2t/t2t/", t2t[key])

    def test_malformed_nonempty_read_input_fails_before_parameter_recommendation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, _ = self._write_inputs(tmpdir)
            malformed = Path(tmpdir) / "malformed.fq"
            malformed.write_text("not fasta or fastq\n", encoding="utf-8")
            state = self._state(tmpdir, assembly, malformed)
            state["stage_outputs"]["t2t"] = {"execution_mode": "local"}

            with patch(
                "app.llm.parameters.base.build_chat_model",
                side_effect=AssertionError(
                    "malformed input must fail before the model"
                ),
            ):
                result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("invalid t2t long-read input", result["last_error"])
            self.assertIsNone(result.get("active_interrupt"))

    def test_one_key_sparse_recommendation_is_preserved_in_approval_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {"execution_mode": "local"}
            with (
                patch("app.llm.parameters.t2t.motif.build_chat_model") as motif_model,
                patch("app.llm.parameters.base.build_chat_model") as build_model,
            ):
                motif_model.return_value.invoke.return_value = SimpleNamespace(
                    content=json.dumps(
                        {"status": "resolved", "telomere_motif": "TTAGGG"}
                    )
                )
                build_model.return_value.invoke.return_value = SimpleNamespace(
                    content=json.dumps(
                        {
                            "parameters": {"min_supporting_reads": 2},
                            "summary": "Require two supporting reads.",
                        }
                    )
                )
                blocked = build_stage().invoke(state)

            request = load_review_request(
                blocked["data_dir"], blocked["active_interrupt"]["request_ref"]
            )
            self.assertEqual(
                request["payload"]["parameters"], {"min_supporting_reads": 2}
            )
            serialized = json.dumps(request["payload"], sort_keys=True)
            self.assertNotIn("omitted_parameters", serialized)
            self.assertNotIn("terminal_window_bp", request["payload"]["parameters"])

    def test_reverse_strand_tail_is_selected_in_target_forward_orientation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            oriented = "A" * 80 + "TTAGGG" * 4
            source = oriented.translate(str.maketrans("ACGT", "TGCA"))[::-1]
            read_id = "clean-a::reverse"
            paf = Path(tmpdir) / "alignment.paf"
            paf.write_text(
                f"{read_id}\t104\t24\t104\t-\tchr1\t100\t0\t90\t80\t80\t60\n",
                encoding="utf-8",
            )
            rows = _candidate_rows(
                paf,
                {
                    "chr1": {
                        "left_has_signal": "true",
                        "right_has_signal": "false",
                    }
                },
                {
                    read_id: {
                        "left_has_signal": "true",
                        "right_has_signal": "false",
                        "source_asset_id": "clean-a",
                        "source_read_id": "reverse",
                    }
                },
                {read_id: source},
                {
                    "min_motif_copies": 4,
                    "min_mapq": 20,
                    "min_overlap_bp": 50,
                    "max_target_terminal_distance_bp": 20,
                    "min_supporting_reads": 1,
                },
                "TTAGGG",
                None,
                "auto_patch",
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["target_end"], "right")
            self.assertEqual(rows[0]["read_end"], "left")
            self.assertEqual(rows[0]["strand"], "-")
            self.assertEqual(rows[0]["oriented_query_end"], "80")
            self.assertEqual(rows[0]["extension_length"], "24")

    def test_junction_trimming_and_conflicting_two_end_patches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "assembly.fa"
            original = "A" * 100
            assembly.write_text(f">chr1\n{original}\n", encoding="utf-8")
            reads = Path(tmpdir) / "telomeric.fa"
            reads.write_text(
                ">left\nTTAGGGAAAA\n>right\nAAAAATTAGGG\n",
                encoding="utf-8",
            )
            state = initial_state(
                session_id="s1",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(v2_run_dir(tmpdir, "s1")),
                workflow_steps=[
                    {
                        "step_id": "step-000-inventory",
                        "stage": "inventory",
                        "allow_local_execution": False,
                    },
                    {
                        "step_id": "step-001-t2t",
                        "stage": "t2t",
                        "allow_local_execution": True,
                    },
                ],
            )
            state["workflow_step_index"] = 1
            state["entered_workflow_step_index"] = 1
            state["current_stage"] = "t2t"
            work = SessionPaths.from_run_dir(state["data_dir"]).workflow_step_work_path(
                "step-001-t2t", "t2t", "t2t_finishing"
            )
            work.mkdir(parents=True)
            candidates = work / "candidates.tsv"

            def write_candidates(left_cut: int, right_cut: int) -> None:
                rows = [
                    {
                        "candidate_id": "chr1:left",
                        "target": "chr1",
                        "target_end": "left",
                        "junction_coordinate": str(left_cut),
                        "read_id": "left",
                        "source_asset_id": "clean-a",
                        "source_read_id": "left",
                        "strand": "+",
                        "oriented_query_start": "6",
                        "oriented_query_end": "10",
                        "supporting_reads": "left",
                        "mapq": "60",
                        "overlap_bp": "4",
                        "decision": "auto_patch",
                    },
                    {
                        "candidate_id": "chr1:right",
                        "target": "chr1",
                        "target_end": "right",
                        "junction_coordinate": str(right_cut),
                        "read_id": "right",
                        "source_asset_id": "clean-a",
                        "source_read_id": "right",
                        "strand": "+",
                        "oriented_query_start": "0",
                        "oriented_query_end": "5",
                        "supporting_reads": "right",
                        "mapq": "60",
                        "overlap_bp": "5",
                        "decision": "auto_patch",
                    },
                ]
                with candidates.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, fieldnames=_CANDIDATE_FIELDS, delimiter="\t"
                    )
                    writer.writeheader()
                    writer.writerows(rows)

            out = {
                "work_dir": str(work),
                "input_assembly_ref": str(assembly),
                "telomeric_reads_fasta_refs": {"map-ont": str(reads)},
                "telomere_extension_candidates_ref": str(candidates),
            }
            execution_inputs = T2TFrozenExecutionInputs(
                thresholds=T2TExecutionThresholds(
                    min_motif_copies=4,
                    terminal_window_bp=10_000,
                    min_mapq=20,
                    max_target_terminal_distance_bp=1_000,
                    min_overlap_bp=500,
                    min_supporting_reads=1,
                ),
                resolved_telomere_motif="TTAGGG",
                motif_source="provided_species_fact",
                mode="auto_patch",
                input_assembly_ref=str(assembly),
                long_read_inputs=(),
                chromosome_list_ref=None,
                work_dir=str(work),
                execution_enabled=True,
            )
            write_candidates(10, 90)
            materialized = _materialize_t2t_patches(
                state,
                out,
                execution_inputs=execution_inputs,
                allowed_decisions={"auto_patch"},
            )
            self.assertIsNotNone(materialized)
            patched = "".join(
                line.strip()
                for line in Path(materialized["patched_assembly_ref"])
                .read_text(encoding="utf-8")
                .splitlines()
                if not line.startswith(">")
            )
            self.assertEqual(patched, "TTAGGG" + original[10:90] + "TTAGGG")
            self.assertEqual(
                assembly.read_text(encoding="utf-8"), f">chr1\n{original}\n"
            )

            other_tmp = tempfile.TemporaryDirectory()
            self.addCleanup(other_tmp.cleanup)
            conflict_state = initial_state(
                session_id="s2",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(v2_run_dir(other_tmp.name, "s2")),
                workflow_steps=[
                    {
                        "step_id": "step-000-inventory",
                        "stage": "inventory",
                        "allow_local_execution": False,
                    },
                    {
                        "step_id": "step-001-t2t",
                        "stage": "t2t",
                        "allow_local_execution": True,
                    },
                ],
            )
            conflict_state["workflow_step_index"] = 1
            conflict_state["entered_workflow_step_index"] = 1
            conflict_state["current_stage"] = "t2t"
            conflict_work = SessionPaths.from_run_dir(
                conflict_state["data_dir"]
            ).workflow_step_work_path("step-001-t2t", "t2t", "t2t_finishing")
            conflict_work.mkdir(parents=True)
            conflict_candidates = conflict_work / "candidates.tsv"
            candidates.replace(conflict_candidates)
            with conflict_candidates.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            rows[0]["junction_coordinate"] = "70"
            rows[1]["junction_coordinate"] = "30"
            with conflict_candidates.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=_CANDIDATE_FIELDS, delimiter="\t"
                )
                writer.writeheader()
                writer.writerows(rows)
            conflict = _materialize_t2t_patches(
                conflict_state,
                {
                    "work_dir": str(conflict_work),
                    "input_assembly_ref": str(assembly),
                    "telomeric_reads_fasta_refs": {"map-ont": str(reads)},
                    "telomere_extension_candidates_ref": str(conflict_candidates),
                },
                execution_inputs=execution_inputs.model_copy(
                    update={"work_dir": str(conflict_work)}
                ),
                allowed_decisions={"auto_patch"},
            )
            self.assertIn("conflicting_junctions", conflict["error"])
            self.assertNotIn("patched_assembly_ref", conflict)
            self.assertTrue(Path(conflict["patch_records_ref"]).is_file())
