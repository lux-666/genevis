"""Focused contracts for the non-annotation execution refactor."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.control.ledger import Ledger
from app.entrypoints.run import _snapshot_resume_decision
from app.execution.artifacts import artifact_ref_for_path, register_artifact_records
from app.execution.env_dispatch import resolve_method_env
from app.execution.env_resolver import EnvResolver, EnvSpec
from app.execution.method_runner import build_frozen_attempt
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec
from app.stages.assembly_dedup.nodes import _read_mapping_preset
from app.stages.assembly_dedup.routing import (
    route_after_enter,
    route_after_plan,
    route_after_run,
)
from app.storage.paths import DataLayoutError, SessionPaths
from tests.data_v2 import v2_run_dir


class ExecutionRefactorContractTests(unittest.TestCase):
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
            self.assertEqual(snapshot.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertTrue(
                paths.to_ref(snapshot).startswith("control/resume_decisions/decision.")
            )

    def test_frozen_attempt_receives_resolved_conda_fields(self):
        attempt = build_frozen_attempt(
            attempt_id="s1:assembly:flye",
            session_id="s1",
            stage="assembly",
            method_name="assembly.candidate.flye",
            script_path=Path("run.sh"),
            working_dir=Path("."),
            expected_outputs=[],
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

            events = Ledger(run_dir).read_events("s1")
            blocked_events = [
                event
                for event in events
                if event["event_type"] == "env_preflight_blocked"
            ]
            self.assertEqual(len(blocked_events), 1)
            self.assertEqual(blocked_events[0]["stage"], "assembly_dedup")
            self.assertEqual(blocked_events[0]["summary"]["status"], "missing_tool")

    def test_wrong_artifact_type_fails_with_data_layout_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(tmpdir, "s1")
            artifact_path = run_dir / "artifacts" / "assembly" / "draft.fasta"
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
                self.assertEqual(route_after_run(state), "fail")

    def test_hifi_reads_use_hifi_mapping_preset(self):
        self.assertEqual(
            _read_mapping_preset({"primary_long_read_platform": "pacbio_hifi"}),
            "map-hifi",
        )
        self.assertEqual(
            _read_mapping_preset({"primary_long_read_platform": "hifi"}),
            "map-hifi",
        )
        self.assertEqual(
            _read_mapping_preset({"primary_long_read_platform": "pacbio_clr"}),
            "map-pb",
        )
        self.assertEqual(
            _read_mapping_preset({"primary_long_read_platform": "ont"}),
            "map-ont",
        )


if __name__ == "__main__":
    unittest.main()
