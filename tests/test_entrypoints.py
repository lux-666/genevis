"""Tests for CLI entrypoint behavior."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.entrypoints.run import _load_dotenv_without_override, _parse_args, _run_ask, _run_inspect, _run_session, generate_session_id
from app.schemas.entry import EntryRequest


class _FakeGraph:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.invocations: list[tuple[dict, dict]] = []

    def invoke(self, state, config):
        self.invocations.append((state, config))
        return self.result


class EntrypointTests(unittest.TestCase):
    def test_stages_flag_is_not_exposed_until_routing_semantics_exist(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            _parse_args(
                [
                    "run",
                    "--session-id",
                    "s1",
                    "--manifest",
                    "manifest.yaml",
                    "--stages",
                    "inventory",
                ]
            )
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_ask_args_parse_minimal_defaults(self):
        args = _parse_args(["ask", "use inputs/manifest.yaml to start s1", "--session-id", "s1", "--data-dir", "out"])

        self.assertEqual(args.command, "ask")
        self.assertEqual(args.request_text, "use inputs/manifest.yaml to start s1")
        self.assertEqual(args.session_id, "s1")
        self.assertEqual(args.data_dir, "out")

    def test_inspect_args_parse_session_id(self):
        args = _parse_args(["inspect", "--session-id", "s1", "--data-dir", "out"])

        self.assertEqual(args.command, "inspect")
        self.assertEqual(args.session_id, "s1")
        self.assertEqual(args.data_dir, "out")

    def test_run_args_can_omit_session_id(self):
        args = _parse_args(["run", "--manifest", "manifest.yaml"])

        self.assertIsNone(args.session_id)
        self.assertEqual(args.manifest, "manifest.yaml")

    def test_generate_session_id_uses_daily_sequence_from_ledger_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            ledger = data_dir / "ledger"
            (ledger / "gene-20260508-000001").mkdir(parents=True)
            (ledger / "gene-20260508-000003").mkdir()
            (ledger / "other-session").mkdir()

            session_id = generate_session_id(data_dir, now=datetime(2026, 5, 8, 10, 0, 0))

        self.assertEqual(session_id, "gene-20260508-000004")

    def test_generate_session_id_starts_at_one_for_new_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = generate_session_id(Path(tmp) / "data", now=datetime(2026, 5, 8, 10, 0, 0))

        self.assertEqual(session_id, "gene-20260508-000001")

    def test_structured_run_generates_session_id_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.run.build_graph", return_value=graph), patch(
                "app.entrypoints.run.initial_state", side_effect=fake_initial_state
            ), patch("app.entrypoints.run.generate_session_id", return_value="gene-20260508-000001"):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    argparse.Namespace(session_id=None, manifest=str(manifest), data_dir=str(root / "data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("session_id: gene-20260508-000001", stdout)
            self.assertEqual(captured["session_id"], "gene-20260508-000001")
            self.assertEqual(graph.invocations[0][1], {"configurable": {"thread_id": "gene-20260508-000001"}})
            self.assertTrue((root / "data" / "ledger" / "gene-20260508-000001" / "session.json").exists())

    def test_structured_run_invokes_graph_without_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            graph = _FakeGraph(
                {
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "route_trace": ["inventory_stage"],
                    "asset_registry_ref": "data/artifacts/s1/assets.json",
                }
            )
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.run.build_graph", return_value=graph), patch(
                "app.entrypoints.run.initial_state", side_effect=fake_initial_state
            ):
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    argparse.Namespace(session_id="s1", manifest=str(manifest), data_dir=str(root / "data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("checkpoint_persistence: memory_only", stdout)
            self.assertIn("resume_note: durable cross-process resume is not available yet", stdout)
            self.assertEqual(
                captured,
                {
                    "session_id": "s1",
                    "input_manifest_ref": str(manifest),
                    "data_dir": str(root / "data"),
                },
            )
            self.assertEqual(graph.invocations[0][1], {"configurable": {"thread_id": "s1"}})
            self.assertTrue((root / "data" / "ledger" / "s1" / "session.json").exists())

    def test_missing_manifest_returns_before_graph_invocation(self):
        with patch("app.entrypoints.run.build_graph") as build_graph:
            code, stdout, stderr = _capture_cli(
                _run_session,
                argparse.Namespace(session_id="s1", manifest="missing.yaml", data_dir="data"),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error: manifest not found", stderr)
        build_graph.assert_not_called()

    def test_blank_manifest_is_rejected_before_path_resolution(self):
        with patch("app.entrypoints.run.build_graph") as build_graph:
            code, stdout, stderr = _capture_cli(
                _run_session,
                argparse.Namespace(session_id="s1", manifest="", data_dir="data"),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid run request", stderr)
        build_graph.assert_not_called()

    def test_path_shaped_session_id_is_rejected_before_ledger_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            with patch("app.entrypoints.run.build_graph") as build_graph:
                code, stdout, stderr = _capture_cli(
                    _run_session,
                    argparse.Namespace(session_id="/tmp/x", manifest=str(manifest), data_dir=str(root / "data")),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("session_id", stderr)
            self.assertFalse((root / "data" / "ledger").exists())
            build_graph.assert_not_called()

    def test_ask_generates_default_session_id_for_parser_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.entrypoints.intent.parse_request_with_llm", side_effect=ValueError("stop")) as parse_request, patch(
                "app.entrypoints.run.generate_session_id", return_value="gene-20260508-000001"
            ):
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    argparse.Namespace(request_text="use manifest", session_id=None, data_dir=str(root / "data")),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("stop", stderr)
            parse_request.assert_called_once_with(
                "use manifest",
                defaults={"session_id": "gene-20260508-000001", "data_dir": str(root / "data")},
            )

    def test_ask_reuses_structured_run_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            request = EntryRequest(session_id="s1", manifest=str(manifest), data_dir=str(root / "data"))
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request) as parse_request, patch(
                "app.entrypoints.run.build_graph", return_value=graph
            ), patch("app.entrypoints.run.initial_state", side_effect=fake_initial_state):
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    argparse.Namespace(request_text="use manifest", session_id="default", data_dir=str(root / "default_data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("checkpoint_persistence: memory_only", stdout)
            parse_request.assert_called_once_with(
                "use manifest",
                defaults={"session_id": "default", "data_dir": str(root / "default_data")},
            )
            self.assertEqual(captured["session_id"], "s1")
            self.assertEqual(captured["input_manifest_ref"], str(manifest))
            self.assertEqual(captured["data_dir"], str(root / "data"))
            self.assertEqual(graph.invocations[0][1], {"configurable": {"thread_id": "s1"}})

    def test_ask_materializes_fasta_source_path_manifest_before_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = root / "assembly.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            request = EntryRequest(session_id="s1", source_paths=[str(assembly)], data_dir=str(root / "data"))
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph", return_value=graph
            ), patch("app.entrypoints.run.initial_state", side_effect=fake_initial_state):
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    argparse.Namespace(request_text=f"use {assembly}", session_id=None, data_dir=str(root / "data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            manifest = Path(captured["input_manifest_ref"])
            self.assertTrue(manifest.exists())
            self.assertEqual(manifest, root / "data" / "manifests" / "s1" / "manifest.yaml")
            text = manifest.read_text(encoding="utf-8")
            self.assertIn("assembly_fasta_registered", text)
            self.assertIn(str(assembly), text)
            self.assertIn("session_id: s1", text)
            self.assertIn("session_id: s1", stdout)

    def test_ask_materializes_hic_alignment_bam_manifest_before_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hic_bam = root / "sample.hic.bam"
            hic_bam.write_text("bam", encoding="utf-8")
            request = EntryRequest(session_id="s1", source_paths=[str(hic_bam)], data_dir=str(root / "data"))
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph", return_value=graph
            ), patch("app.entrypoints.run.initial_state", side_effect=fake_initial_state):
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    argparse.Namespace(request_text=f"use {hic_bam}", session_id=None, data_dir=str(root / "data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            text = Path(captured["input_manifest_ref"]).read_text(encoding="utf-8")
            self.assertIn("hic_alignment_bam", text)
            self.assertIn(str(hic_bam), text)
            self.assertIn("session_id: s1", stdout)

    def test_ask_materializes_plain_bam_as_raw_long_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bam = root / "sample.bam"
            bam.write_text("bam", encoding="utf-8")
            request = EntryRequest(session_id="s1", source_paths=[str(bam)], data_dir=str(root / "data"))
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph", return_value=graph
            ), patch("app.entrypoints.run.initial_state", side_effect=fake_initial_state):
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    argparse.Namespace(request_text=f"use {bam}", session_id=None, data_dir=str(root / "data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            text = Path(captured["input_manifest_ref"]).read_text(encoding="utf-8")
            self.assertIn("raw_long_reads", text)
            self.assertNotIn("hic_alignment_bam", text)

    def test_ask_materializes_folder_source_paths_manifest_before_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reads_dir = root / "reads"
            reads_dir.mkdir()
            read1 = reads_dir / "cycloneseq_1.fastq"
            read2 = reads_dir / "cycloneseq_2.fastq"
            read1.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            read2.write_text("@r2\nTGCA\n+\n!!!!\n", encoding="utf-8")
            request = EntryRequest(session_id="s1", source_paths=[str(reads_dir)], data_dir=str(root / "data"))
            graph = _FakeGraph({"current_stage": "done", "supervision_state": "completed", "route_trace": []})
            captured = {}

            def fake_initial_state(**kwargs):
                captured.update(kwargs)
                return {"session_id": kwargs["session_id"]}

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph", return_value=graph
            ), patch("app.entrypoints.run.initial_state", side_effect=fake_initial_state):
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    argparse.Namespace(request_text=f"use {reads_dir}", session_id=None, data_dir=str(root / "data")),
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            manifest = Path(captured["input_manifest_ref"])
            text = manifest.read_text(encoding="utf-8")
            self.assertIn("raw_long_reads", text)
            self.assertIn("cycloneseq", text)
            self.assertIn(str(read1), text)
            self.assertIn(str(read2), text)
            self.assertIn("session_id: s1", stdout)

    def test_ask_rejects_unsupported_source_file_before_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes.txt"
            notes.write_text("not a biological input\n", encoding="utf-8")
            request = EntryRequest(session_id="s1", source_paths=[str(notes)], data_dir=str(root / "data"))

            with patch("app.entrypoints.intent.parse_request_with_llm", return_value=request), patch(
                "app.entrypoints.run.build_graph"
            ) as build_graph:
                code, stdout, stderr = _capture_cli(
                    _run_ask,
                    argparse.Namespace(request_text=f"use {notes}", session_id=None, data_dir=str(root / "data")),
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("unsupported source file type", stderr)
            build_graph.assert_not_called()

    def test_ask_lifecycle_failure_does_not_invoke_graph(self):
        with patch("app.entrypoints.intent.parse_request_with_llm", side_effect=ValueError("Lifecycle requests")), patch(
            "app.entrypoints.run.build_graph"
        ) as build_graph:
            code, stdout, stderr = _capture_cli(
                _run_ask,
                argparse.Namespace(request_text="resume s1", session_id=None, data_dir=None),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Lifecycle requests", stderr)
        build_graph.assert_not_called()

    def test_inspect_missing_session_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="missing", data_dir=str(Path(tmp) / "data")),
            )

        self.assertEqual(code, 1)
        self.assertIn("checkpoint_persistence: memory_only", stdout)
        self.assertIn("durable_resume_available: false", stdout)
        self.assertIn("session_not_found", stderr)

    def test_inspect_existing_session_prints_memory_only_limitation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            from app.control.ledger import Ledger

            ledger = Ledger(data_dir)
            ledger.create_session("s1", "manifest.yaml")
            ledger.record_review_requested("s1", "assembly_review", reason="needs_review")
            code, stdout, stderr = _capture_cli(
                _run_inspect,
                argparse.Namespace(session_id="s1", data_dir=str(data_dir)),
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("supervision_state: blocked_human", stdout)
        self.assertIn("durable_resume_available: false", stdout)
        self.assertIn("latest_review_request", stdout)

    def test_dotenv_loader_uses_gene_agent_local_env_without_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "GENE_AGENT_LLM_API_KEY=sk-from-dotenv\nGENE_AGENT_LLM_MODEL=from-dotenv\n",
                encoding="utf-8",
            )
            old_key = os.environ.get("GENE_AGENT_LLM_API_KEY")
            old_model = os.environ.get("GENE_AGENT_LLM_MODEL")
            try:
                os.environ["GENE_AGENT_LLM_API_KEY"] = "sk-existing"
                os.environ.pop("GENE_AGENT_LLM_MODEL", None)
                with patch("os.getcwd", return_value=str(root)):
                    _load_dotenv_without_override()
                self.assertEqual(os.environ["GENE_AGENT_LLM_API_KEY"], "sk-existing")
                self.assertEqual(os.environ["GENE_AGENT_LLM_MODEL"], "from-dotenv")
            finally:
                if old_key is None:
                    os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
                else:
                    os.environ["GENE_AGENT_LLM_API_KEY"] = old_key
                if old_model is None:
                    os.environ.pop("GENE_AGENT_LLM_MODEL", None)
                else:
                    os.environ["GENE_AGENT_LLM_MODEL"] = old_model


def _capture_cli(func, *args):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = func(*args)
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
