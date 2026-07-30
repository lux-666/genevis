"""Focused contracts for immutable control-input freezing."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.entrypoints.run import _run_session, execute_frozen_session
from app.execution.dependency_snapshot import (
    capture_dependency_snapshot,
    load_frozen_dependency_resolver,
)
from app.execution.env_dispatch import resolve_method_env
from app.execution.env_resolver import PreflightResult
from app.storage.frozen_inputs import (
    FrozenInputConflictError,
    FrozenInputIntegrityError,
    FrozenInputValidationError,
    freeze_session_inputs,
    frozen_execution_lock,
    validate_frozen_inputs,
)
from app.storage.paths import SessionPaths, ensure_run_layout


class _FrozenExecutionGraph:
    def __init__(self) -> None:
        self.invocations: list[tuple[object, dict]] = []

    def invoke(self, state, config):
        self.invocations.append((state, config))
        return {
            "current_stage": "done",
            "supervision_state": "completed",
            "route_trace": [],
        }


class FrozenInputTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[SessionPaths, Path, Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        reads = root / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        manifest = root / "manifest_draft.yaml"
        manifest.write_text(
            "session_id: s1\n"
            "assets:\n"
            "  - asset_type: raw_long_reads\n"
            "    paths: [reads.fastq]\n"
            "    platform: ont\n"
            "    format: fastq\n",
            encoding="utf-8",
        )
        run_config = root / "run_config_draft.json"
        run_config.write_text("{}\n", encoding="utf-8")
        envs = root / "envs.yaml"
        envs.write_text(
            "default:\n  fallback_env: original-env\n"
            "stages:\n  assembly:\n    conda_env: original-assembly\n"
            "packages: {}\n",
            encoding="utf-8",
        )
        return SessionPaths(root / "data", "s1"), manifest, run_config, envs

    def test_freeze_is_create_only_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))

            first = freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            marker_before = paths.frozen_inputs_path.read_bytes()

            self.assertTrue(paths.input_manifest_path.is_file())
            self.assertTrue(paths.run_config_path.is_file())
            self.assertTrue(paths.dependency_manifest_path.is_file())
            self.assertEqual(
                first.manifest.assets[0].paths,
                [str((Path(tmp) / "reads.fastq").resolve())],
            )
            self.assertEqual(
                first.marker.files["input_manifest"].path_ref,
                "control/input_manifest.yaml",
            )

            second = freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            self.assertEqual(marker_before, paths.frozen_inputs_path.read_bytes())
            self.assertEqual(second.marker, first.marker)
            self.assertEqual(
                validate_frozen_inputs(paths).run_config,
                second.run_config,
            )

    def test_partial_transaction_and_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )

            paths.input_manifest_path.write_text(
                paths.input_manifest_path.read_text(encoding="utf-8") + "# tampered\n",
                encoding="utf-8",
            )
            with self.assertRaises(FrozenInputIntegrityError):
                validate_frozen_inputs(paths)
            with self.assertRaises(FrozenInputIntegrityError):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                )

            fresh_paths, fresh_manifest, fresh_config, fresh_envs = self._fixture(
                Path(tmp) / "fresh"
            )
            ensure_run_layout(fresh_paths)
            fresh_paths.input_manifest_path.write_text("partial\n", encoding="utf-8")
            with self.assertRaises(FrozenInputIntegrityError):
                freeze_session_inputs(
                    fresh_paths,
                    manifest_draft=fresh_manifest,
                    run_config_draft=fresh_config,
                    config_path=fresh_envs,
                )

    def test_different_draft_cannot_replace_frozen_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            changed = Path(tmp) / "changed.yaml"
            changed.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "reads.fastq", "other.fastq"
                ),
                encoding="utf-8",
            )
            (Path(tmp) / "other.fastq").write_text(
                "@r2\nTGCA\n+\n!!!!\n", encoding="utf-8"
            )
            with self.assertRaises(FrozenInputConflictError):
                freeze_session_inputs(
                    paths,
                    manifest_draft=changed,
                    run_config_draft=run_config,
                    config_path=envs,
                )

    def test_different_dependency_selection_cannot_reuse_frozen_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            envs.write_text(
                envs.read_text(encoding="utf-8")
                + "methods:\n"
                + "  assembly.candidate.flye: {conda_env: flye-env}\n"
                + "  assembly.candidate.hifiasm: {conda_env: hifiasm-env}\n",
                encoding="utf-8",
            )
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
                selected_stages=["assembly"],
                selected_methods={"assembly": ["assembly.candidate.flye"]},
            )

            with self.assertRaises(FrozenInputConflictError):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                    selected_stages=["assembly"],
                    selected_methods={"assembly": ["assembly.candidate.hifiasm"]},
                )

    def test_selected_method_must_belong_to_its_declared_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))

            with self.assertRaises(FrozenInputValidationError):
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                    selected_stages=["assembly"],
                    selected_methods={
                        "assembly": ["gene_annotation.candidate.miniprot"]
                    },
                )

    def test_freeze_normalizes_explicit_paired_hic_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            hic_dir = root / "hic"
            hic_dir.mkdir()
            (hic_dir / "R1.fastq.gz").write_text("R1\n", encoding="utf-8")
            (hic_dir / "R2.fastq.gz").write_text("R2\n", encoding="utf-8")
            manifest.write_text(
                manifest.read_text(encoding="utf-8")
                + "  - asset_type: hic_reads_registered\n"
                + "    platform: illumina\n"
                + "    layout: paired\n"
                + "    format: fastq\n"
                + "    paired_reads:\n"
                + "      - r1: hic/R1.fastq.gz\n"
                + "        r2: hic/R2.fastq.gz\n",
                encoding="utf-8",
            )

            bundle = freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )

            hic_asset = next(
                asset
                for asset in bundle.manifest.assets
                if asset.asset_type == "hic_reads_registered"
            )
            expected = [
                str((hic_dir / "R1.fastq.gz").resolve()),
                str((hic_dir / "R2.fastq.gz").resolve()),
            ]
            self.assertEqual(hic_asset.paths, expected)
            self.assertEqual(hic_asset.paired_reads[0].r1, expected[0])
            self.assertEqual(hic_asset.paired_reads[0].r2, expected[1])

    def test_dependency_resolver_uses_snapshot_after_live_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
                selected_stages=["assembly"],
                selected_methods={},
            )
            envs.write_text(
                "default:\n  fallback_env: changed-live-env\n"
                "stages:\n  assembly:\n    conda_env: changed-live-assembly\n"
                "packages: {}\n",
                encoding="utf-8",
            )

            resolver = load_frozen_dependency_resolver(paths.run_dir)
            self.assertEqual(
                resolver.resolve(stage="assembly").conda_env, "original-assembly"
            )
            with patch(
                "app.execution.env_dispatch.preflight",
                return_value=PreflightResult(
                    status="ok", env_target="original-assembly"
                ),
            ):
                spec, packages, blocked = resolve_method_env(
                    {},
                    stage="assembly",
                    method_name="assembly.unknown",
                    session_id="s1",
                    data_dir=paths.run_dir,
                    input_manifest_ref="control/input_manifest.yaml",
                    config_path=envs,
                )
            self.assertIsNone(blocked)
            self.assertEqual(spec.conda_env if spec else None, "original-assembly")
            self.assertEqual(packages, [])

    def test_execute_frozen_session_uses_verified_canonical_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            run_config.write_text(
                json.dumps({"local_exec": ["read_qc"]}) + "\n",
                encoding="utf-8",
            )
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            envs.unlink()

            graph = _FrozenExecutionGraph()
            captured: dict = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"], "stage_outputs": {}}

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.initial_state", side_effect=fake_initial_state
                ),
                patch("app.entrypoints.run.finalize_terminal_state", return_value=None),
                patch("app.entrypoints.run.materialize_run_layout") as materialize,
                patch("app.entrypoints.run.load_run_config") as load_run_config,
                patch(
                    "app.execution.dependency_snapshot.EnvResolver",
                    side_effect=AssertionError("live config must not be loaded"),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = execute_frozen_session(
                    "s1",
                    paths.data_root,
                    checkpointer="memory",
                    interactive_gate=False,
                )

            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            materialize.assert_not_called()
            load_run_config.assert_not_called()
            self.assertEqual(
                captured["input_manifest_ref"],
                "control/input_manifest.yaml",
            )
            self.assertEqual(captured["data_dir"], str(paths.run_dir))
            invoked_state = graph.invocations[0][0]
            self.assertEqual(
                invoked_state["stage_outputs"]["read_qc"]["execution_mode"],
                "local",
            )

    def test_execution_lock_refuses_duplicate_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            stderr = io.StringIO()

            with (
                frozen_execution_lock(paths),
                patch("app.entrypoints.run.build_graph") as build_graph,
                contextlib.redirect_stderr(stderr),
            ):
                code = execute_frozen_session(
                    "s1",
                    paths.data_root,
                    checkpointer="sqlite",
                    interactive_gate=False,
                )

            self.assertEqual(code, 1)
            self.assertIn("already active", stderr.getvalue())
            build_graph.assert_not_called()

    def test_checkpoint_execution_continues_without_fresh_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            paths.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(paths.checkpoint_db_path)
            try:
                connection.execute(
                    "CREATE TABLE checkpoints ("
                    "thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, "
                    "checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, "
                    "type TEXT, checkpoint BLOB, metadata BLOB, "
                    "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
                )
                connection.execute(
                    "INSERT INTO checkpoints VALUES (?, '', 'cp1', NULL, "
                    "'msgpack', ?, ?)",
                    ("s1", b"checkpoint", b"metadata"),
                )
                connection.commit()
            finally:
                connection.close()

            graph = _FrozenExecutionGraph()
            with (
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch("app.entrypoints.run.finalize_terminal_state", return_value=None),
            ):
                code = execute_frozen_session(
                    "s1",
                    paths.data_root,
                    checkpointer="sqlite",
                    interactive_gate=False,
                )

            self.assertEqual(code, 0)
            self.assertIsNone(graph.invocations[0][0])

    def test_partial_execution_without_checkpoint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, manifest, run_config, envs = self._fixture(Path(tmp))
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            paths.ledger_events_path.parent.mkdir(parents=True, exist_ok=True)
            paths.ledger_events_path.write_text("partial\n", encoding="utf-8")
            stderr = io.StringIO()

            with (
                patch("app.entrypoints.run.build_graph") as build_graph,
                contextlib.redirect_stderr(stderr),
            ):
                code = execute_frozen_session(
                    "s1",
                    paths.data_root,
                    checkpointer="sqlite",
                    interactive_gate=False,
                )

            self.assertEqual(code, 1)
            self.assertIn("without a resumable checkpoint", stderr.getvalue())
            build_graph.assert_not_called()

    def test_dependency_snapshot_uses_selected_execution_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected_root = root / "selected-package"
            internal_root = root / "internal-package"
            unrelated_root = root / "unrelated-package"
            for package_root in (selected_root, internal_root, unrelated_root):
                package_root.mkdir()
                (package_root / "canary").write_text("ok\n", encoding="utf-8")
            envs = root / "envs.yaml"
            envs.write_text(
                "default:\n  fallback_env: default-env\n"
                "stages:\n"
                "  assembly: {conda_env: assembly-env}\n"
                "  assembly_dedup: {conda_env: dedup-env}\n"
                "  gene_annotation: {conda_env: gene-env}\n"
                "  repeat_annotation: {conda_env: unrelated-env}\n"
                "methods:\n"
                "  assembly.candidate.flye:\n"
                "    conda_env: flye-env\n"
                "    package_tools: [selected]\n"
                "  assembly.candidate.hifiasm:\n"
                "    conda_env: hifiasm-env\n"
                "    package_tools: [unrelated]\n"
                "  assembly_dedup.purge_dups:\n"
                "    conda_env: dedup-method-env\n"
                "  gene_annotation.candidate.miniprot:\n"
                "    conda_env: miniprot-env\n"
                "  gene_annotation.materialize.gffread:\n"
                "    conda_env: gffread-env\n"
                "    package_tools: [internal]\n"
                "  gene_annotation.merge.evm:\n"
                "    conda_env: evm-env\n"
                "    package_tools: [internal]\n"
                "  gene_annotation.busco:\n"
                "    conda_env: gene-busco-env\n"
                "    package_tools: [internal]\n"
                "  repeat_annotation.masking.repeatmasker:\n"
                "    conda_env: repeat-env\n"
                "    package_tools: [unrelated]\n"
                "packages:\n"
                "  selected:\n"
                f"    install_root: {selected_root}\n"
                "    canary_paths: [canary]\n"
                "  internal:\n"
                f"    install_root: {internal_root}\n"
                "    canary_paths: [canary]\n"
                "  unrelated:\n"
                f"    install_root: {unrelated_root}\n"
                "    canary_paths: [canary]\n",
                encoding="utf-8",
            )

            snapshot = capture_dependency_snapshot(
                config_path=envs,
                stages=["assembly", "assembly_dedup", "gene_annotation"],
                methods=[
                    "assembly.candidate.flye",
                    "gene_annotation.candidate.miniprot",
                ],
            )

            self.assertEqual(
                set(snapshot.stage_environments),
                {"assembly", "assembly_dedup", "gene_annotation"},
            )
            self.assertEqual(
                set(snapshot.method_environments),
                {
                    "assembly.candidate.flye",
                    "assembly_dedup.purge_dups",
                    "gene_annotation.candidate.miniprot",
                    "gene_annotation.materialize.gffread",
                    "gene_annotation.merge.evm",
                    "gene_annotation.busco",
                },
            )
            self.assertEqual(
                snapshot.selected_stages,
                ["assembly", "assembly_dedup", "gene_annotation"],
            )
            self.assertEqual(
                snapshot.selected_methods,
                ["assembly.candidate.flye", "gene_annotation.candidate.miniprot"],
            )
            self.assertEqual(set(snapshot.packages), {"selected", "internal"})

    def test_execute_frozen_session_rejects_any_tampered_control_input(self) -> None:
        targets = (
            "input_manifest_path",
            "run_config_path",
            "dependency_manifest_path",
        )
        for target_name in targets:
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                paths, manifest, run_config, envs = self._fixture(Path(tmp))
                freeze_session_inputs(
                    paths,
                    manifest_draft=manifest,
                    run_config_draft=run_config,
                    config_path=envs,
                )
                target = getattr(paths, target_name)
                target.write_bytes(target.read_bytes() + b"\n")
                stderr = io.StringIO()

                with (
                    patch("app.entrypoints.run.build_graph") as build_graph,
                    patch("app.entrypoints.run.materialize_run_layout") as materialize,
                    contextlib.redirect_stderr(stderr),
                ):
                    code = execute_frozen_session(
                        "s1",
                        paths.data_root,
                        checkpointer="memory",
                        interactive_gate=False,
                    )

                self.assertEqual(code, 1)
                self.assertIn("frozen session cannot execute", stderr.getvalue())
                detail = stderr.getvalue()
                self.assertTrue(
                    "hash mismatch" in detail or "does not match marker" in detail,
                    detail,
                )
                build_graph.assert_not_called()
                materialize.assert_not_called()

    def test_frozen_run_branch_rejects_noncanonical_input_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, manifest, run_config, envs = self._fixture(root)
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                config_path=envs,
            )
            alternate_manifest = root / "alternate-manifest.yaml"
            alternate_manifest.write_bytes(paths.input_manifest_path.read_bytes())
            alternate_run_config = root / "alternate-run-config.json"
            alternate_run_config.write_bytes(paths.run_config_path.read_bytes())
            cases = (
                (alternate_manifest, paths.run_config_path),
                (paths.input_manifest_path, alternate_run_config),
            )

            for supplied_manifest, supplied_run_config in cases:
                with self.subTest(
                    manifest=supplied_manifest,
                    run_config=supplied_run_config,
                ):
                    stderr = io.StringIO()
                    with (
                        patch("app.entrypoints.run.build_graph") as build_graph,
                        patch(
                            "app.entrypoints.run.materialize_run_layout"
                        ) as materialize,
                        patch("app.entrypoints.run.load_run_config") as load_run_config,
                        contextlib.redirect_stderr(stderr),
                    ):
                        code = _run_session(
                            **dict(
                                session_id="s1",
                                manifest=str(supplied_manifest),
                                data_dir=str(paths.data_root),
                                run_config=str(supplied_run_config),
                                local_exec=[],
                                checkpointer="memory",
                                frozen_session=True,
                                interactive_gate=False,
                            )
                        )

                    self.assertEqual(code, 1)
                    self.assertIn("frozen session cannot execute", stderr.getvalue())
                    build_graph.assert_not_called()
                    materialize.assert_not_called()
                    load_run_config.assert_not_called()
