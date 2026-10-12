"""Focused contracts for the shared execution refactor."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from app.entrypoints.run import _snapshot_resume_decision
from app.execution.env_dispatch import resolve_method_env
from app.execution.env_resolver import EnvResolver, EnvSpec
from app.execution.method_runner import build_frozen_attempt
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import initial_state
from app.stages.assembly_dedup.nodes import _minimap2_preset
from app.stages.assembly_dedup.routing import (
    route_after_enter,
    route_after_plan,
    route_after_purge_dups,
)
from app.stages.inventory.nodes import validate_inputs
from app.storage.artifacts import artifact_ref_for_path, register_artifact_records
from app.storage.frozen_inputs import freeze_session_inputs, validate_frozen_inputs
from app.storage.ledger import Ledger
from app.storage.paths import DataLayoutError, SessionPaths
from tests.busco_fixture import ensure_test_host_envs
from tests.data_v2 import v2_run_dir


class ExecutionRefactorContractTests(unittest.TestCase):
    def test_run_manifest_resolves_relative_assets_against_source_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            source_dir.mkdir()
            reads = source_dir / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            source_manifest = source_dir / "manifest.yaml"
            source_manifest.write_text(
                "session_id: s1\n"
                "assets:\n"
                "  - asset_type: raw_long_reads\n"
                "    paths: [reads.fastq]\n"
                "    platform: ont\n"
                "    format: fastq\n",
                encoding="utf-8",
            )
            source_config = source_dir / "run_config.json"
            source_config.write_text(
                json.dumps({"requested_stages": []}),
                encoding="utf-8",
            )
            paths = SessionPaths(root / "data", "s1")
            envs = ensure_test_host_envs(root)

            freeze_session_inputs(
                paths,
                manifest_draft=source_manifest,
                run_config_draft=source_config,
                config_path=envs,
                selected_methods={},
            )

            canonical = yaml.safe_load(
                paths.input_manifest_path.read_text(encoding="utf-8")
            )
            self.assertEqual(canonical["assets"][0]["paths"], [str(reads.resolve())])
            state = initial_state(
                session_id="s1",
                input_manifest_ref=paths.to_ref(paths.input_manifest_path),
                data_dir=str(paths.run_dir),
            )
            inventory = validate_inputs(
                state,
                verified_inputs=validate_frozen_inputs(paths),
            )["stage_outputs"]["inventory"]
            self.assertTrue(inventory["valid"])

    def test_run_config_paths_resolve_against_source_config_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            source_dir.mkdir()
            reads_dir = source_dir / "reads"
            reads_dir.mkdir()
            shared_dir = root / "shared"
            shared_dir.mkdir()
            assembly_dir = source_dir / "assembly"
            assembly_dir.mkdir()
            hifi_reads = reads_dir / "hifi.fastq"
            hifi_reads.write_text("@hifi\nACGT\n+\n!!!!\n", encoding="utf-8")
            (reads_dir / "hifi.fofn").write_text(f"{hifi_reads}\n", encoding="utf-8")
            (reads_dir / "ont-a.fastq.gz").write_bytes(
                gzip.compress(b"@ont\nACGT\n+\n!!!!\n")
            )
            (shared_dir / "hifi-b.fastq.gz").write_bytes(
                gzip.compress(b"@hifi\nACGT\n+\n!!!!\n")
            )
            (assembly_dir / "final.fasta").write_text(
                ">contig\nACGT\n", encoding="utf-8"
            )
            source_manifest = source_dir / "manifest.yaml"
            source_manifest.write_text(
                "session_id: s1\n"
                "assets:\n"
                "  - asset_type: assembly_fasta_registered\n"
                "    paths: [assembly/final.fasta]\n",
                encoding="utf-8",
            )
            source_config = source_dir / "run_config.json"
            source_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [],
                        "assembly": {"hifi_reads_ref": "reads/hifi.fofn"},
                        "assembly_review": {"busco": {"threads": 4}},
                        "t2t": {
                            "assembly_fasta_ref": "assembly/final.fasta",
                            "read_inputs": [
                                {
                                    "path": "reads/ont-a.fastq.gz",
                                    "platform": "ont",
                                },
                                {
                                    "path": "../shared/hifi-b.fastq.gz",
                                    "platform": "pacbio_hifi",
                                },
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            paths = SessionPaths(root / "data", "s1")
            envs = ensure_test_host_envs(root)

            freeze_session_inputs(
                paths,
                manifest_draft=source_manifest,
                run_config_draft=source_config,
                config_path=envs,
                selected_methods={},
            )

            canonical = json.loads(paths.run_config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                canonical["assembly"]["hifi_reads_ref"],
                str((source_dir / "reads/hifi.fofn").resolve()),
            )
            self.assertEqual(canonical["assembly_review"]["busco"]["threads"], 4)
            self.assertEqual(
                canonical["t2t"]["assembly_fasta_ref"],
                str((source_dir / "assembly/final.fasta").resolve()),
            )
            self.assertEqual(
                canonical["t2t"]["read_inputs"],
                [
                    {
                        "path": str((source_dir / "reads/ont-a.fastq.gz").resolve()),
                        "platform": "ont",
                    },
                    {
                        "path": str(
                            (source_dir / "../shared/hifi-b.fastq.gz").resolve()
                        ),
                        "platform": "pacbio_hifi",
                    },
                ],
            )

    def test_external_resume_decision_is_snapshotted_under_run_control(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = v2_run_dir(root / "data", "s1")
            source = root / "operator-decision.json"
            source.write_text('{"decision":"stop_human_review"}\n', encoding="utf-8")
            paths = SessionPaths.from_run_dir(run_dir)
            decision = ResumeReviewRequest(
                session_id="s1",
                stage="assembly_review",
                decision="stop_human_review",
            )

            snapshot = _snapshot_resume_decision(source, paths, decision)

            self.assertEqual(snapshot.parent, paths.control_dir / "resume_decisions")
            self.assertRegex(snapshot.name, r"^decision\.[0-9a-f]{16}\.json$")
            self.assertEqual(
                snapshot.read_text(encoding="utf-8"), source.read_text(encoding="utf-8")
            )
            self.assertTrue(
                paths.to_ref(snapshot).startswith("control/resume_decisions/decision.")
            )

    def test_frozen_attempt_receives_resolved_conda_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            working_dir = Path(tmpdir)
            script_path = working_dir / "run.sh"
            script_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            attempt = build_frozen_attempt(
                attempt_id="s1:assembly:flye",
                session_id="s1",
                stage="assembly",
                method_name="assembly.candidate.flye",
                script_path=script_path,
                working_dir=working_dir,
                expected_outputs=[],
                input_dependencies=[],
                env=None,
                env_spec=EnvSpec(conda_bin="conda-test", conda_env="gene-tools"),
            )

            self.assertIsInstance(attempt, FrozenAttemptSpec)
            self.assertEqual(attempt.conda_bin, "conda-test")
            self.assertEqual(attempt.conda_env, "gene-tools")
            self.assertIsNone(attempt.conda_prefix)

    def test_undefined_package_tool_is_a_configuration_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "envs.yaml"
            config.write_text(
                "default:\n"
                "  fallback_env: base\n"
                "methods:\n"
                "  assembly_dedup.purge_dups:\n"
                "    package_tools: [missing_package]\n",
                encoding="utf-8",
            )

            resolver = EnvResolver(config)
            spec = resolver.resolve(
                stage="assembly_dedup",
                method_name="assembly_dedup.purge_dups",
            )

            with self.assertRaisesRegex(ValueError, "undefined package_tools"):
                resolver.packages_for_spec(spec)

    def test_path_override_package_failure_is_recorded_in_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = v2_run_dir(root, "s1")
            config = root / "envs.yaml"
            config.write_text(
                "default:\n"
                "  fallback_env: base\n"
                "packages:\n"
                "  juicer:\n"
                "    install_root: /path/that/does/not/exist\n"
                "    canary_paths: [juicer.sh]\n"
                "methods:\n"
                "  assembly_dedup.purge_dups:\n"
                "    package_tools: [juicer]\n",
                encoding="utf-8",
            )

            spec, packages, blocked = resolve_method_env(
                {"env": {"PATH": "/tmp/fake-bin"}},
                stage="assembly_dedup",
                method_name="assembly_dedup.purge_dups",
                workflow_step_id="step-001-assembly_dedup",
                session_id="s1",
                data_dir=run_dir,
                input_manifest_ref="control/manifest.yaml",
                config_path=config,
            )

            self.assertIsNone(spec)
            self.assertEqual(packages, [])
            self.assertIsNotNone(blocked)
            assert blocked is not None
            self.assertEqual(blocked["status"], "missing_tool")
            self.assertEqual(blocked["method_name"], "assembly_dedup.purge_dups")

            _spec, _packages, second_blocked = resolve_method_env(
                {"env": {"PATH": "/tmp/fake-bin"}},
                stage="assembly_dedup",
                method_name="assembly_dedup.purge_dups",
                workflow_step_id="step-002-assembly_dedup",
                session_id="s1",
                data_dir=run_dir,
                input_manifest_ref="control/manifest.yaml",
                config_path=config,
            )
            self.assertIsNotNone(second_blocked)

            events = Ledger(run_dir).read_events("s1")
            blocked_events = [
                event
                for event in events
                if event["event_type"] == "env_preflight_blocked"
            ]
            self.assertEqual(len(blocked_events), 2)
            self.assertEqual(blocked_events[0]["stage"], "assembly_dedup")
            self.assertEqual(blocked_events[0]["summary"]["status"], "missing_tool")
            self.assertEqual(
                [event["workflow_step_id"] for event in blocked_events],
                ["step-001-assembly_dedup", "step-002-assembly_dedup"],
            )

    def test_path_override_is_rejected_after_frozen_inputs_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reads = root / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\n"
                "assets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths: [{reads}]\n"
                "    platform: ont\n"
                "    format: fastq\n",
                encoding="utf-8",
            )
            run_config = root / "run_config.json"
            run_config.write_text('{"requested_stages": []}\n', encoding="utf-8")
            envs = root / "envs.yaml"
            envs.write_text(
                "default:\n  fallback_env: original-env\n"
                "stages:\n  assembly:\n    conda_env: original-assembly\n"
                "packages: {}\n",
                encoding="utf-8",
            )
            run_dir = v2_run_dir(root / "data", "s1")
            freeze_session_inputs(
                SessionPaths.from_run_dir(run_dir),
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
                selected_methods={},
            )

            spec, packages, blocked = resolve_method_env(
                {"env": {"PATH": "/tmp/untrusted-stage-bin"}},
                stage="assembly",
                method_name="assembly.candidate.flye",
                workflow_step_id="step-001-assembly",
                session_id="s1",
                data_dir=run_dir,
                input_manifest_ref="control/input_manifest.yaml",
                config_path=envs,
            )

            self.assertIsNone(spec)
            self.assertEqual(packages, [])
            self.assertIsNotNone(blocked)
            assert blocked is not None
            self.assertEqual(blocked["status"], "blocked_env")
            self.assertIn("PATH override is rejected", blocked["reason"])

    def test_wrong_artifact_type_fails_with_data_layout_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(tmpdir, "s1")
            paths = SessionPaths.from_run_dir(run_dir)
            artifact_path = paths.workflow_step_artifact_path(
                "step-001-assembly",
                "assembly",
                "outputs",
                "draft.fasta",
            )
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(">contig\nACGT\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                artifact_path,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="assembly.draft",
            )
            register_artifact_records(run_dir, "s1", [artifact])

            with self.assertRaisesRegex(DataLayoutError, "expected.type"):
                artifact_ref_for_path(
                    run_dir,
                    "s1",
                    artifact_path,
                    expected_type="expected.type",
                )

    def test_dedup_routes_all_failure_statuses_to_fail(self):
        for status in ("failed", "blocked_env", "missing_tool"):
            with self.subTest(status=status):
                state = {"stage_outputs": {"assembly_dedup": {"status": status}}}
                self.assertEqual(route_after_enter(state), "fail")
                self.assertEqual(route_after_plan(state), "fail")
                self.assertEqual(route_after_purge_dups(state), "fail")

    def test_long_read_platforms_use_matching_minimap2_presets(self):
        self.assertEqual(_minimap2_preset("pacbio_hifi"), "map-hifi")
        self.assertEqual(_minimap2_preset("pacbio_clr"), "map-pb")
        self.assertEqual(_minimap2_preset("ont"), "map-ont")
        self.assertEqual(_minimap2_preset("cycloneseq"), "map-ont")


if __name__ == "__main__":
    unittest.main()
