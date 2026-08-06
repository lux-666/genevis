"""Focused contract tests for the deterministic ``gene-agent run`` command."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app.entrypoints.run as run_entry
from app.control.resume_transaction import ResumeTransactionError
from app.graph.builder import build_graph
from app.schemas.run_config import RunConfig
from app.storage.paths import SessionPaths


def _capture_call(function, *args, **kwargs):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        result = function(*args, **kwargs)
    return result, stdout.getvalue(), stderr.getvalue()


def _prepared_inputs(
    *,
    session_id: str = "manifest-session",
    requested_stages: list[str] | None = None,
    explicit_fields: frozenset[str] = frozenset({"requested_stages", "local_exec"}),
):
    return SimpleNamespace(
        manifest=SimpleNamespace(session_id=session_id),
        run_config=RunConfig(
            requested_stages=requested_stages,
            local_exec=[],
        ),
        explicit_run_config_fields=explicit_fields,
    )


class RunParserTests(unittest.TestCase):
    def test_run_parser_exposes_exact_approved_namespace(self):
        args = run_entry._parse_args(
            [
                "run",
                "--manifest",
                "manifest.yaml",
                "--run-config",
                "run.json",
                "--data-dir",
                "data",
                "--non-interactive",
            ]
        )

        self.assertEqual(
            vars(args),
            {
                "command": "run",
                "manifest": "manifest.yaml",
                "run_config": "run.json",
                "data_dir": "data",
                "non_interactive": True,
            },
        )

    def test_help_advertises_run_and_only_approved_run_options(self):
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as top_exit:
            with contextlib.redirect_stdout(stdout):
                run_entry._parse_args(["--help"])
        self.assertEqual(top_exit.exception.code, 0)
        self.assertIn("run", stdout.getvalue())

        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as run_exit:
            with contextlib.redirect_stdout(stdout):
                run_entry._parse_args(["run", "--help"])
        self.assertEqual(run_exit.exception.code, 0)
        help_text = stdout.getvalue()
        for option in (
            "--manifest",
            "--run-config",
            "--data-dir",
            "--non-interactive",
        ):
            self.assertIn(option, help_text)
        for forbidden in (
            "--session-id",
            "--checkpointer",
            "--local-exec",
        ):
            self.assertNotIn(forbidden, help_text)

    def test_run_requires_all_three_paths_and_argparse_returns_two(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(stderr):
                run_entry._parse_args(["run", "--manifest", "manifest.yaml"])

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--run-config", stderr.getvalue())
        self.assertIn("--data-dir", stderr.getvalue())

    def test_run_rejects_legacy_and_unapproved_arguments(self):
        base = [
            "run",
            "--manifest",
            "manifest.yaml",
            "--run-config",
            "run.json",
            "--data-dir",
            "data",
        ]
        for extra in (
            ["--session-id", "cli-owned"],
            ["--checkpointer", "memory"],
            ["--local-exec", "assembly"],
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit) as caught:
                    with contextlib.redirect_stderr(io.StringIO()):
                        run_entry._parse_args([*base, *extra])
                self.assertEqual(caught.exception.code, 2)

        for command in ("launch", "ask"):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit) as caught:
                    with contextlib.redirect_stderr(io.StringIO()):
                        run_entry._parse_args([command])
                self.assertEqual(caught.exception.code, 2)


class RunHandlerTests(unittest.TestCase):
    def test_manifest_identity_owns_paths_and_sqlite_dispatch(self):
        prepared = _prepared_inputs(requested_stages=["assembly"])
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                manifest="source/manifest.yaml",
                run_config="source/run.json",
                data_dir=tmp,
                non_interactive=False,
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ) as prepare,
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch(
                    "app.entrypoints.run.execute_frozen_session", return_value=0
                ) as execute,
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=True,
                ),
            ):
                code = run_entry._run(args)

        self.assertEqual(code, 0)
        prepare.assert_called_once_with(
            manifest_draft="source/manifest.yaml",
            run_config_draft="source/run.json",
        )
        frozen_paths = freeze.call_args.args[0]
        self.assertEqual(frozen_paths.session_id, "manifest-session")
        self.assertEqual(frozen_paths.data_root, Path(tmp).resolve())
        self.assertEqual(
            freeze.call_args.kwargs,
            {
                "prepared_inputs": prepared,
                "selected_stages": ["assembly"],
                "selected_methods": {},
            },
        )
        execute.assert_called_once_with(
            "manifest-session",
            Path(tmp).resolve(),
            checkpointer="sqlite",
            interactive_gate=True,
        )

    def test_noninteractive_flag_maps_to_false(self):
        prepared = _prepared_inputs(requested_stages=["read_qc"])
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                manifest="manifest.yaml",
                run_config="run.json",
                data_dir=tmp,
                non_interactive=True,
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch("app.storage.frozen_inputs.freeze_session_inputs"),
                patch(
                    "app.entrypoints.run.execute_frozen_session", return_value=0
                ) as execute,
            ):
                code = run_entry._run(args)

        self.assertEqual(code, 0)
        self.assertFalse(execute.call_args.kwargs["interactive_gate"])

    def test_non_tty_run_does_not_start_foreground_review(self):
        prepared = _prepared_inputs(requested_stages=["read_qc"])
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                manifest="manifest.yaml",
                run_config="run.json",
                data_dir=tmp,
                non_interactive=False,
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch("app.storage.frozen_inputs.freeze_session_inputs"),
                patch(
                    "app.entrypoints.run._foreground_review_available",
                    return_value=False,
                ),
                patch(
                    "app.entrypoints.run.execute_frozen_session", return_value=0
                ) as execute,
            ):
                code = run_entry._run(args)

        self.assertEqual(code, 0)
        self.assertFalse(execute.call_args.kwargs["interactive_gate"])

    def test_empty_requested_stage_scope_is_frozen_as_empty(self):
        prepared = _prepared_inputs(requested_stages=[])
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                manifest="manifest.yaml",
                run_config="run.json",
                data_dir=tmp,
                non_interactive=False,
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch("app.entrypoints.run.execute_frozen_session", return_value=0),
            ):
                code = run_entry._run(args)

        self.assertEqual(code, 0)
        self.assertEqual(freeze.call_args.kwargs["selected_stages"], [])
        self.assertIsNotNone(freeze.call_args.kwargs["selected_stages"])

    def test_missing_explicit_fields_fail_before_freeze_or_execute(self):
        for explicit_fields, missing in (
            (frozenset({"local_exec"}), "requested_stages"),
            (frozenset({"requested_stages"}), "local_exec"),
            (frozenset(), "local_exec, requested_stages"),
        ):
            with self.subTest(explicit_fields=explicit_fields):
                prepared = _prepared_inputs(
                    requested_stages=[],
                    explicit_fields=explicit_fields,
                )
                args = argparse.Namespace(
                    manifest="manifest.yaml",
                    run_config="run.json",
                    data_dir="data",
                    non_interactive=False,
                )
                with (
                    patch(
                        "app.storage.frozen_inputs.prepare_session_inputs",
                        return_value=prepared,
                    ),
                    patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                    patch("app.entrypoints.run.execute_frozen_session") as execute,
                ):
                    code, stdout, stderr = _capture_call(run_entry._run, args)

                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("error:", stderr)
                self.assertIn(missing, stderr)
                freeze.assert_not_called()
                execute.assert_not_called()

    def test_null_requested_stages_fails_before_freeze_or_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = root / "assembly.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "session_id: explicit-null-plan\n"
                "assets:\n"
                "  - asset_type: assembly_fasta_registered\n"
                f"    paths: [{assembly}]\n",
                encoding="utf-8",
            )
            run_config = root / "run_config.json"
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": None,
                        "local_exec": [],
                    }
                ),
                encoding="utf-8",
            )
            data_dir = root / "data"
            args = argparse.Namespace(
                manifest=str(manifest),
                run_config=str(run_config),
                data_dir=str(data_dir),
                non_interactive=False,
            )

            with (
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch("app.entrypoints.run.execute_frozen_session") as execute,
            ):
                code, stdout, stderr = _capture_call(run_entry._run, args)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(
            "requested_stages must be a JSON array, not null",
            stderr,
        )
        self.assertFalse(data_dir.exists())
        freeze.assert_not_called()
        execute.assert_not_called()

    def test_preflight_failures_are_normalized(self):
        args = argparse.Namespace(
            manifest="manifest.yaml",
            run_config="run.json",
            data_dir="data",
            non_interactive=False,
        )
        with (
            patch(
                "app.storage.frozen_inputs.prepare_session_inputs",
                side_effect=ValueError("malformed run config"),
            ),
            patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
            patch("app.entrypoints.run.execute_frozen_session") as execute,
        ):
            code, stdout, stderr = _capture_call(run_entry._run, args)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "error: run preflight failed: malformed run config\n",
        )
        freeze.assert_not_called()
        execute.assert_not_called()

    def test_noncanonical_manifest_session_id_fails_before_freeze(self):
        prepared = _prepared_inputs(
            session_id="./manifest-session",
            requested_stages=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            args = argparse.Namespace(
                manifest="manifest.yaml",
                run_config="run.json",
                data_dir=str(data_root),
                non_interactive=False,
            )
            with (
                patch(
                    "app.storage.frozen_inputs.prepare_session_inputs",
                    return_value=prepared,
                ),
                patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
                patch("app.entrypoints.run.execute_frozen_session") as execute,
            ):
                code, stdout, stderr = _capture_call(run_entry._run, args)

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("session_id must be one path component", stderr)
            self.assertFalse(data_root.exists())
            freeze.assert_not_called()
            execute.assert_not_called()

    def test_main_dispatches_run_handler(self):
        argv = [
            "run",
            "--manifest",
            "manifest.yaml",
            "--run-config",
            "run.json",
            "--data-dir",
            "data",
        ]
        with (
            patch("app.entrypoints.run._load_dotenv_without_override"),
            patch("app.entrypoints.run._run", return_value=7) as run_handler,
        ):
            code = run_entry.main(argv)

        self.assertEqual(code, 7)
        run_handler.assert_called_once()
        self.assertEqual(run_handler.call_args.args[0].command, "run")


class PublicRunSQLiteIntegrationTests(unittest.TestCase):
    def test_noninteractive_run_persists_and_reopens_blocked_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "public-run-sqlite-gate"
            assembly = root / "existing_assembly.fa"
            assembly.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")

            manifest = root / "manifest.yaml"
            manifest.write_text(
                "\n".join(
                    [
                        f"session_id: {session_id}",
                        "sample_id: sqlite-gate-sample",
                        "assets:",
                        "  - asset_type: assembly_fasta_registered",
                        f"    paths: [{assembly}]",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            run_config = root / "run_config.json"
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": ["assembly_review"],
                        "local_exec": [],
                    }
                ),
                encoding="utf-8",
            )
            data_dir = root / "data"

            code, stdout, stderr = _capture_call(
                run_entry.main,
                [
                    "run",
                    "--manifest",
                    str(manifest),
                    "--run-config",
                    str(run_config),
                    "--data-dir",
                    str(data_dir),
                    "--non-interactive",
                ],
            )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertIn("supervision_state: blocked_human", stdout)
            self.assertIn("checkpoint_persistence: sqlite", stdout)

            paths = SessionPaths(data_dir, session_id)
            self.assertTrue(paths.checkpoint_db_path.is_file())
            with sqlite3.connect(paths.checkpoint_db_path) as connection:
                checkpoint_count = connection.execute(
                    "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
                    (session_id,),
                ).fetchone()[0]
            self.assertGreater(checkpoint_count, 0)

            reopened_graph = build_graph(
                backend="sqlite",
                data_dir=str(paths.run_dir),
            )
            try:
                snapshot = reopened_graph.get_state(
                    {"configurable": {"thread_id": session_id}}
                )
                self.assertEqual(
                    snapshot.values["supervision_state"],
                    "blocked_human",
                )
                self.assertEqual(snapshot.values["current_stage"], "assembly_review")
                self.assertIsNotNone(snapshot.values["active_interrupt"])
            finally:
                saver = reopened_graph.checkpointer
                if hasattr(saver, "conn"):
                    saver.conn.close()


class FrozenExecutionBoundaryTests(unittest.TestCase):
    def test_validation_progress_and_lock_delegate_verified_bundle(self):
        bundle = SimpleNamespace()

        @contextlib.contextmanager
        def fake_lock(_paths):
            yield

        with tempfile.TemporaryDirectory() as tmp:
            expected_paths = SessionPaths(tmp, "frozen-session")
            with (
                patch(
                    "app.storage.frozen_inputs.frozen_execution_lock",
                    side_effect=fake_lock,
                ) as lock,
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=bundle,
                ) as validate,
                patch(
                    "app.storage.frozen_inputs.frozen_execution_progress",
                    return_value="checkpoint",
                ) as progress,
                patch(
                    "app.entrypoints.run._execute_verified_frozen_session",
                    return_value=0,
                ) as execute,
            ):
                code = run_entry.execute_frozen_session(
                    "frozen-session",
                    tmp,
                    checkpointer="memory",
                    interactive_gate=False,
                )

        self.assertEqual(code, 0)
        lock.assert_called_once_with(expected_paths)
        validate.assert_called_once_with(expected_paths)
        progress.assert_called_once_with(expected_paths)
        execute.assert_called_once_with(
            bundle,
            checkpointer="memory",
            resume_checkpoint=True,
            interactive_gate=False,
        )

    def test_partial_execution_is_normalized_before_helper(self):
        @contextlib.contextmanager
        def fake_lock(_paths):
            yield

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "app.storage.frozen_inputs.frozen_execution_lock",
                    side_effect=fake_lock,
                ),
                patch(
                    "app.storage.frozen_inputs.validate_frozen_inputs",
                    return_value=SimpleNamespace(),
                ),
                patch(
                    "app.storage.frozen_inputs.frozen_execution_progress",
                    return_value="partial",
                ),
                patch(
                    "app.entrypoints.run._execute_verified_frozen_session"
                ) as execute,
            ):
                code, stdout, stderr = _capture_call(
                    run_entry.execute_frozen_session,
                    "frozen-session",
                    tmp,
                )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error:", stderr)
        self.assertIn("partial execution evidence", stderr)
        execute.assert_not_called()


class FrozenHelperTests(unittest.TestCase):
    def test_frozen_run_injects_effective_wait_into_every_registered_review_stage(self):
        from app.review.gates import registered_review_stages

        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig.model_validate(
                    {
                        "requested_stages": [],
                        "review_policy": {
                            "max_blocked_seconds": 300,
                            "stages": {
                                "hic_scaffolding": {"max_blocked_seconds": -1},
                                "t2t": {"max_blocked_seconds": 0},
                            },
                        },
                    }
                ),
            )
            graph = Mock()
            graph.invoke.return_value = {
                "current_stage": "done",
                "supervision_state": "completed",
                "route_trace": [],
            }
            with (
                patch("app.entrypoints.run.Ledger"),
                patch(
                    "app.entrypoints.run.initial_state",
                    return_value={"stage_outputs": {}},
                ),
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch("app.entrypoints.run.finalize_terminal_state", return_value=None),
            ):
                code, _stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=False,
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        invoked_state = graph.invoke.call_args.args[0]
        stage_outputs = invoked_state["stage_outputs"]
        self.assertEqual(set(stage_outputs), set(registered_review_stages()))
        for stage in registered_review_stages():
            expected = 300
            if stage == "hic_scaffolding":
                expected = -1
            elif stage == "t2t":
                expected = 0
            self.assertEqual(
                stage_outputs[stage]["review_policy"]["max_blocked_seconds"],
                expected,
            )

    def test_noninteractive_block_uses_frozen_authority_and_prints_guidance(self):
        with tempfile.TemporaryDirectory(prefix="gene agent ") as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(requested_stages=[], local_exec=[]),
            )
            graph = Mock()
            graph.invoke.return_value = {
                "current_stage": "assembly_review",
                "supervision_state": "blocked_human",
                "route_trace": ["assembly_stage"],
                "active_interrupt": {"kind": "review"},
            }
            seeded_state = {
                "session_id": "frozen-session",
                "stage_outputs": {},
            }

            with (
                patch("app.entrypoints.run.Ledger") as ledger,
                patch(
                    "app.entrypoints.run.initial_state", return_value=seeded_state
                ) as initial,
                patch("app.entrypoints.run.build_graph", return_value=graph) as build,
                patch(
                    "app.entrypoints.run.finalize_terminal_state", return_value=None
                ) as finalize,
                patch("app.entrypoints.run._interactive_gate_loop") as gate_loop,
            ):
                code, stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=False,
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        initial.assert_called_once_with(
            session_id="frozen-session",
            input_manifest_ref="control/input_manifest.yaml",
            data_dir=str(paths.run_dir),
            requested_stages=[],
        )
        ledger.assert_called_once_with(str(paths.run_dir))
        ledger.return_value.create_session.assert_called_once_with(
            "frozen-session", "control/input_manifest.yaml"
        )
        build.assert_called_once_with(backend="sqlite", data_dir=str(paths.run_dir))
        graph.invoke.assert_called_once_with(
            seeded_state,
            config={"configurable": {"thread_id": "frozen-session"}},
        )
        gate_loop.assert_not_called()
        finalize.assert_called_once()
        self.assertIn("supervision_state: blocked_human", stdout)
        self.assertIn("inspect_command: gene-agent inspect", stdout)
        self.assertIn("review_command: gene-agent review", stdout)
        self.assertIn("resume_command: gene-agent resume", stdout)
        self.assertIn("--session-id frozen-session", stdout)
        self.assertIn("--data-dir", stdout)
        self.assertIn(f"'{paths.data_root}'", stdout)
        self.assertIn(
            f"run_config_ref: {paths.run_config_path.resolve(strict=False)}", stdout
        )

    def test_none_requested_stage_scope_remains_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(requested_stages=None, local_exec=[]),
            )
            graph = Mock()
            graph.invoke.return_value = {
                "current_stage": "done",
                "supervision_state": "completed",
                "route_trace": [],
            }
            with (
                patch("app.entrypoints.run.Ledger"),
                patch(
                    "app.entrypoints.run.initial_state",
                    return_value={"stage_outputs": {}},
                ) as initial,
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch("app.entrypoints.run.finalize_terminal_state", return_value=None),
            ):
                code, _stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=False,
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIsNone(initial.call_args.kwargs["requested_stages"])

    def test_interactive_resume_validation_failure_returns_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(requested_stages=[], local_exec=[]),
            )
            graph = Mock()
            graph.invoke.return_value = {
                "current_stage": "assembly_review",
                "supervision_state": "blocked_human",
                "route_trace": [],
            }
            with (
                patch("app.entrypoints.run.Ledger"),
                patch(
                    "app.entrypoints.run.initial_state",
                    return_value={"stage_outputs": {}},
                ),
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run._interactive_gate_loop",
                    side_effect=ResumeTransactionError(
                        "validation",
                        "stale review decision",
                    ),
                ),
                patch(
                    "app.entrypoints.run.finalize_terminal_state",
                    return_value=None,
                ) as finalize,
            ):
                code, stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=True,
                )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "error: resume validation failed: stale review decision\n",
        )
        finalize.assert_not_called()

    def test_existing_blocked_checkpoint_does_not_restore_foreground_countdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(requested_stages=[]),
            )
            blocked_state = {
                "session_id": "frozen-session",
                "current_stage": "assembly_review",
                "supervision_state": "blocked_human",
                "route_trace": [],
                "active_interrupt": {
                    "stage": "assembly_review",
                    "gate_id": "assembly_review",
                    "review_request_id": "review-existing",
                },
            }
            graph = Mock()
            graph.get_state.return_value = SimpleNamespace(values=blocked_state)
            with (
                patch("app.entrypoints.run.Ledger"),
                patch(
                    "app.entrypoints.run.initial_state",
                    return_value={"stage_outputs": {}},
                ),
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch("app.entrypoints.run._interactive_gate_loop") as gate_loop,
                patch(
                    "app.entrypoints.run.finalize_terminal_state",
                    return_value=None,
                ),
            ):
                code, stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=True,
                    interactive_gate=True,
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        graph.invoke.assert_not_called()
        gate_loop.assert_not_called()
        self.assertIn("countdown is not restored across processes", stdout)
        self.assertIn("review_command: gene-agent review", stdout)

    def test_graph_exception_persists_normalized_terminal_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "frozen-session")
            bundle = SimpleNamespace(
                paths=paths,
                run_config=RunConfig(requested_stages=["assembly"], local_exec=[]),
            )
            graph = Mock()
            graph.invoke.side_effect = RuntimeError("stage exploded")
            graph.get_state.return_value = SimpleNamespace(
                values={"current_stage": "assembly", "route_trace": ["inventory"]}
            )
            with (
                patch("app.entrypoints.run.Ledger"),
                patch("app.entrypoints.run.build_graph", return_value=graph),
                patch(
                    "app.entrypoints.run.finalize_terminal_state", return_value=None
                ) as finalize,
            ):
                code, stdout, stderr = _capture_call(
                    run_entry._execute_verified_frozen_session,
                    bundle,
                    checkpointer="sqlite",
                    resume_checkpoint=False,
                    interactive_gate=False,
                )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(
            "error: workflow invoke failed: RuntimeError: stage exploded", stderr
        )
        failure = finalize.call_args.args[2]
        self.assertEqual(failure["session_id"], "frozen-session")
        self.assertEqual(failure["data_dir"], str(paths.run_dir))
        self.assertEqual(failure["input_manifest_ref"], "control/input_manifest.yaml")
        self.assertEqual(failure["supervision_state"], "failed_terminal")
        self.assertEqual(failure["last_error"], "RuntimeError: stage exploded")
        self.assertEqual(failure["checkpoint_persistence"], "sqlite")
        self.assertEqual(failure["current_stage"], "assembly")
        self.assertEqual(
            finalize.call_args.kwargs,
            {"report_kind": "operator"},
        )


if __name__ == "__main__":
    unittest.main()
