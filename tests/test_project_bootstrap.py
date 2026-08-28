"""Focused tests for minimal project bootstrap initialization."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app.entrypoints.run import (
    _load_dotenv_without_override,
    _parse_args,
    _run_bootstrap,
)
from app.execution.env_resolver import EnvResolver
from app.execution.package_bootstrap import verify_envs_yaml
from app.execution.project_bootstrap import (
    ProjectConfigChange,
    apply_project_bootstrap_plan,
    build_project_bootstrap_plan,
    collect_project_bootstrap_plan,
)


class ProjectBootstrapTests(unittest.TestCase):
    def test_cli_creates_consumable_autohic_config_and_redacts_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            package_roots, error_model, chromosome_model = _autohic_fixture(root)
            database_paths = _database_fixture(root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_conda = fake_bin / "conda"
            fake_conda.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_conda.chmod(0o755)
            previous_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{fake_bin}{os.pathsep}{previous_path}"
            previous_key = os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
            try:
                answers = [
                    "tools",
                    "y",
                    str(database_paths["busco"]),
                    str(database_paths["ncbi"]),
                    "y",
                    str(package_roots["autohic"]),
                    str(package_roots["juicer"]),
                    str(package_roots["three_d_dna"]),
                    str(package_roots["juicer_tools"]),
                    str(error_model),
                    str(chromosome_model),
                    "y",
                    "",
                    "",
                    "",
                ]
                with (
                    patch("builtins.input", side_effect=answers),
                    patch("getpass.getpass", return_value="sk-bootstrap-secret"),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = _run_bootstrap(argparse.Namespace(config=str(config_path)))

                self.assertEqual(code, 0, stderr.getvalue())
                self.assertTrue(config_path.is_file())
                resolver = EnvResolver(config_path)
                self.assertEqual(resolver.resolve(stage="unknown").conda_env, "tools")
                self.assertIsNotNone(resolver.databases)
                assert resolver.databases is not None
                self.assertEqual(
                    resolver.databases.busco.default_lineage,
                    "metazoa_odb10",
                )
                self.assertEqual(
                    resolver.databases.busco.lineages["metazoa_odb10"].version,
                    "odb10",
                )
                self.assertEqual(
                    set(resolver.packages),
                    {"autohic", "juicer", "three_d_dna", "juicer_tools"},
                )
                self.assertEqual(
                    resolver.packages["autohic"].model_path("error"),
                    error_model,
                )
                self.assertEqual(
                    resolver.packages["autohic"].model_path("chromosome"),
                    chromosome_model,
                )

                dotenv_path = root / ".env"
                self.assertEqual(stat.S_IMODE(dotenv_path.stat().st_mode), 0o600)
                self.assertIn(
                    "GENE_AGENT_LLM_API_KEY=sk-bootstrap-secret",
                    dotenv_path.read_text(encoding="utf-8"),
                )
                self.assertNotIn("sk-bootstrap-secret", stdout.getvalue())
                self.assertIn("<redacted>", stdout.getvalue())
                self.assertIn("package conformance: juicer", stdout.getvalue())
                self.assertLess(
                    stdout.getvalue().index("package conformance: juicer"),
                    stdout.getvalue().index("Candidate configuration is valid"),
                )
                self.assertEqual(
                    os.environ["GENE_AGENT_LLM_API_KEY"], "sk-bootstrap-secret"
                )
            finally:
                os.environ["PATH"] = previous_path
                if previous_key is None:
                    os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
                else:
                    os.environ["GENE_AGENT_LLM_API_KEY"] = previous_key

    def test_cli_can_initialize_before_optional_resources_are_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_conda = fake_bin / "conda"
            fake_conda.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_conda.chmod(0o755)
            previous_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{fake_bin}{os.pathsep}{previous_path}"
            previous_key = os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
            try:
                with (
                    patch(
                        "builtins.input",
                        side_effect=[
                            "tools",
                            "n",
                            "n",
                            "y",
                            "https://llm.example/v1",
                            "test-model",
                            "",
                        ],
                    ),
                    patch("getpass.getpass", return_value="sk-later-resources"),
                ):
                    code = _run_bootstrap(argparse.Namespace(config=str(config_path)))

                self.assertEqual(code, 0)
                document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                self.assertNotIn("databases", document)
                self.assertEqual(document["packages"], {})
                self.assertNotIn(
                    "hic_scaffolding.candidate.autohic",
                    document["methods"],
                )
                self.assertIn(
                    "GENE_AGENT_LLM_MODEL=test-model",
                    (root / ".env").read_text(encoding="utf-8"),
                )
            finally:
                os.environ["PATH"] = previous_path
                if previous_key is None:
                    os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
                else:
                    os.environ["GENE_AGENT_LLM_API_KEY"] = previous_key

    def test_rerun_is_idempotent_and_does_not_prompt_for_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                yaml.safe_dump(_minimal_host_config(root), sort_keys=False),
                encoding="utf-8",
            )
            before = config_path.read_bytes()

            with patch(
                "builtins.input",
                side_effect=AssertionError("valid config must not prompt"),
            ):
                code = _run_bootstrap(argparse.Namespace(config=str(config_path)))

            self.assertEqual(code, 0)
            self.assertEqual(config_path.read_bytes(), before)

    def test_interactive_bootstrap_can_add_dotenv_to_existing_valid_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                yaml.safe_dump(_minimal_host_config(root), sort_keys=False),
                encoding="utf-8",
            )
            previous_key = os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
            try:
                # no AutoHiC package setup, create .env, no URL/model overrides
                with (
                    patch("sys.stdin.isatty", return_value=True),
                    patch("builtins.input", side_effect=["n", "y", "", "", ""]),
                    patch("getpass.getpass", return_value="sk-existing-project"),
                ):
                    code = _run_bootstrap(argparse.Namespace(config=str(config_path)))

                self.assertEqual(code, 0)
                self.assertEqual(
                    (root / ".env").read_text(encoding="utf-8"),
                    "GENE_AGENT_LLM_API_KEY=sk-existing-project\n",
                )
            finally:
                if previous_key is None:
                    os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
                else:
                    os.environ["GENE_AGENT_LLM_API_KEY"] = previous_key

    def test_existing_values_are_preserved_without_overwrite_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            document = _minimal_host_config(root)
            document["default"] = {
                "conda_bin": "micromamba",
                "fallback_env": "existing",
            }
            document["custom"] = {"keep": "value"}
            config_path.write_text(
                yaml.safe_dump(document, sort_keys=False),
                encoding="utf-8",
            )

            plan = build_project_bootstrap_plan(
                config_path,
                config_values={"shared_conda_target": "replacement"},
                configure_autohic=False,
            )

            self.assertFalse(plan.config_changed)
            self.assertEqual(plan.config_data["default"]["fallback_env"], "existing")
            self.assertEqual(plan.config_data["custom"], {"keep": "value"})

    def test_existing_pre_database_config_collects_one_migration_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: tools\npackages: {}\n",
                encoding="utf-8",
            )
            databases = _database_fixture(root)
            answers = iter(
                [
                    "y",
                    str(databases["busco"]),
                    str(databases["ncbi"]),
                    "n",
                    "n",
                ]
            )

            plan = collect_project_bootstrap_plan(
                config_path,
                prompt=lambda _message: next(answers),
            )

            self.assertEqual(plan.unresolved_fields, [])
            self.assertEqual(
                plan.config_data["databases"]["busco"]["default_lineage"],
                "metazoa_odb10",
            )
            self.assertEqual(
                plan.config_data["databases"]["ncbi"]["reference_dir"],
                str(databases["ncbi"]),
            )

    def test_plain_bootstrap_can_add_databases_to_manual_partial_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n"
                "  conda_bin: /usr/bin/true\n"
                "  fallback_env: tools\n"
                "packages: {}\n"
                "stages: {}\n"
                "methods: {}\n",
                encoding="utf-8",
            )
            databases = _database_fixture(root)

            with (
                patch("sys.stdin.isatty", return_value=True),
                patch(
                    "builtins.input",
                    side_effect=[
                        "y",
                        str(databases["busco"]),
                        str(databases["ncbi"]),
                        "n",
                        "n",
                        "",
                    ],
                ),
            ):
                code = _run_bootstrap(argparse.Namespace(config=str(config_path)))

            self.assertEqual(code, 0)
            resolver = EnvResolver(config_path)
            self.assertIsNotNone(resolver.databases)
            assert resolver.databases is not None
            self.assertEqual(
                resolver.databases.busco.default_lineage,
                "metazoa_odb10",
            )

    def test_update_replaces_only_selected_existing_field_on_final_enter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n"
                "  conda_bin: conda\n"
                "  fallback_env: existing\n"
                "packages: {}\n"
                "custom:\n"
                "  keep: value\n",
                encoding="utf-8",
            )
            _attach_database_registry(config_path, root)
            stdout = io.StringIO()

            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=["2", "replacement", ""]),
                contextlib.redirect_stdout(stdout),
            ):
                code = _run_bootstrap(
                    argparse.Namespace(
                        config=str(config_path),
                        update=True,
                    )
                )

            self.assertEqual(code, 0)
            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["default"]["conda_bin"], "conda")
            self.assertEqual(parsed["default"]["fallback_env"], "replacement")
            self.assertEqual(parsed["custom"], {"keep": "value"})
            self.assertIn("existing -> replacement", stdout.getvalue())

    def test_update_rejects_stale_plan_without_losing_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: existing\npackages: {}\n",
                encoding="utf-8",
            )
            _attach_database_registry(config_path, root)
            responses = iter(["2", "replacement"])

            def answer(_prompt: str = "") -> str:
                try:
                    return next(responses)
                except StopIteration:
                    config_path.write_text(
                        "default:\n  fallback_env: existing\n"
                        "packages: {}\ncustom:\n  concurrent: kept\n",
                        encoding="utf-8",
                    )
                    return ""

            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=answer),
            ):
                code = _run_bootstrap(
                    argparse.Namespace(
                        config=str(config_path),
                        update=True,
                    )
                )

            self.assertEqual(code, 1)
            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["default"]["fallback_env"], "existing")
            self.assertEqual(parsed["custom"], {"concurrent": "kept"})

    def test_update_rejects_unrecorded_candidate_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: existing\npackages: {}\n",
                encoding="utf-8",
            )
            before = config_path.read_bytes()
            plan = build_project_bootstrap_plan(
                config_path,
                config_values={"shared_conda_target": "replacement"},
                configure_autohic=False,
                approved_overwrites={"default.fallback_target"},
            )
            plan.config_data["custom"] = {"unrecorded": "value"}

            with self.assertRaisesRegex(ValueError, "unrecorded config change"):
                apply_project_bootstrap_plan(plan)

            self.assertEqual(config_path.read_bytes(), before)

    def test_update_preview_uses_unset_to_new_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: existing\npackages: {}\n",
                encoding="utf-8",
            )
            _attach_database_registry(config_path, root)
            stdout = io.StringIO()

            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=["1", "micromamba", ""]),
                contextlib.redirect_stdout(stdout),
            ):
                code = _run_bootstrap(
                    argparse.Namespace(
                        config=str(config_path),
                        update=True,
                    )
                )

            self.assertEqual(code, 0)
            self.assertIn("<unset> -> micromamba", stdout.getvalue())

    def test_update_requires_existing_config_without_prompting(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config" / "envs.yaml"

            with patch(
                "builtins.input",
                side_effect=AssertionError("missing config must not prompt"),
            ):
                code = _run_bootstrap(
                    argparse.Namespace(
                        config=str(config_path),
                        update=True,
                    )
                )

            self.assertEqual(code, 1)
            self.assertFalse(config_path.exists())

    def test_update_rejects_invalid_unselected_method_before_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: existing\n"
                "methods:\n"
                "  assembly.candidate.flye:\n"
                "    conda_env: flye\n"
                "    conda_prefix: /envs/flye\n"
                "packages: {}\n",
                encoding="utf-8",
            )
            before = config_path.read_bytes()

            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=["1", "micromamba"]),
            ):
                code = _run_bootstrap(
                    argparse.Namespace(
                        config=str(config_path),
                        update=True,
                    )
                )

            self.assertEqual(code, 1)
            self.assertEqual(config_path.read_bytes(), before)

    def test_dotenv_update_does_not_run_unselected_package_conformance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            juicer = root / "juicer"
            tools = root / "juicer-tools"
            (juicer / "CPU").mkdir(parents=True)
            (juicer / "CPU/juicer.sh").write_text("ok\n", encoding="utf-8")
            tools.mkdir()
            (tools / "juicer_tools.jar").write_text("jar\n", encoding="utf-8")
            config_path.write_text(
                "default:\n  fallback_env: existing\n"
                "packages:\n"
                "  juicer:\n"
                f"    install_root: {juicer}\n"
                "    canary_paths: [CPU/juicer.sh]\n"
                "    conform_on_bootstrap: true\n"
                "    jar_root_ref: juicer_tools\n"
                "  juicer_tools:\n"
                f"    install_root: {tools}\n"
                "    canary_paths: [juicer_tools.jar]\n",
                encoding="utf-8",
            )
            _attach_database_registry(config_path, root)

            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=["5", "new-model", ""]),
            ):
                code = _run_bootstrap(
                    argparse.Namespace(
                        config=str(config_path),
                        update=True,
                    )
                )

            self.assertEqual(code, 0)
            self.assertFalse((juicer / "scripts").exists())
            self.assertIn(
                "GENE_AGENT_LLM_MODEL=new-model",
                (root / ".env").read_text(encoding="utf-8"),
            )

    def test_update_interrupt_at_confirmation_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: existing\npackages: {}\n",
                encoding="utf-8",
            )
            _attach_database_registry(config_path, root)
            before = config_path.read_bytes()
            stdout = io.StringIO()

            with (
                patch("sys.stdin.isatty", return_value=True),
                patch(
                    "builtins.input",
                    side_effect=["2", "replacement", KeyboardInterrupt()],
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = _run_bootstrap(
                    argparse.Namespace(
                        config=str(config_path),
                        update=True,
                    )
                )

            self.assertEqual(code, 1)
            self.assertEqual(config_path.read_bytes(), before)
            self.assertIn("existing -> replacement", stdout.getvalue())
            self.assertIn("cancelled", stdout.getvalue())

    def test_update_eof_at_any_prompt_does_not_write(self):
        prompt_sequences = (
            [EOFError()],
            ["2", EOFError()],
            ["2", "replacement", EOFError()],
        )
        for responses in prompt_sequences:
            with (
                self.subTest(responses=responses),
                tempfile.TemporaryDirectory() as tmp,
            ):
                config_path = Path(tmp) / "config" / "envs.yaml"
                config_path.parent.mkdir()
                config_path.write_text(
                    "default:\n  fallback_env: existing\npackages: {}\n",
                    encoding="utf-8",
                )
                before = config_path.read_bytes()
                with (
                    patch("sys.stdin.isatty", return_value=True),
                    patch("builtins.input", side_effect=responses),
                ):
                    code = _run_bootstrap(
                        argparse.Namespace(
                            config=str(config_path),
                            update=True,
                        )
                    )

                self.assertEqual(code, 1)
                self.assertEqual(config_path.read_bytes(), before)

    def test_update_redacts_existing_llm_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: tools\npackages: {}\n",
                encoding="utf-8",
            )
            _attach_database_registry(config_path, root)
            dotenv_path = root / ".env"
            dotenv_path.write_text("GENE_AGENT_LLM_API_KEY=old-secret\n")
            dotenv_path.chmod(0o644)
            stdout = io.StringIO()
            previous_key = os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
            try:
                with (
                    patch("sys.stdin.isatty", return_value=True),
                    patch("builtins.input", side_effect=["3", ""]),
                    patch("getpass.getpass", return_value="new-secret"),
                    contextlib.redirect_stdout(stdout),
                ):
                    code = _run_bootstrap(
                        argparse.Namespace(
                            config=str(config_path),
                            update=True,
                        )
                    )

                self.assertEqual(code, 0)
                self.assertEqual(
                    dotenv_path.read_text(encoding="utf-8"),
                    "GENE_AGENT_LLM_API_KEY=new-secret\n",
                )
                self.assertNotIn("old-secret", stdout.getvalue())
                self.assertNotIn("new-secret", stdout.getvalue())
                self.assertIn("<redacted> -> <redacted>", stdout.getvalue())
                self.assertEqual(stat.S_IMODE(dotenv_path.stat().st_mode), 0o600)
            finally:
                if previous_key is None:
                    os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
                else:
                    os.environ["GENE_AGENT_LLM_API_KEY"] = previous_key

    def test_bootstrap_parser_has_one_confirmed_write_flow(self):
        self.assertTrue(_parse_args(["bootstrap", "--update"]).update)
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            _parse_args(["bootstrap", "--apply"])

    def test_update_invalid_package_path_is_rejected_without_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            package_roots, error_model, chromosome_model = _autohic_fixture(root)
            database_values = _database_values(root)
            (package_roots["juicer"] / "scripts").symlink_to(
                "CPU", target_is_directory=True
            )
            (package_roots["juicer"] / "CPU/common/juicer_tools.jar").symlink_to(
                package_roots["juicer_tools"] / "juicer_tools.jar"
            )
            plan = build_project_bootstrap_plan(
                config_path,
                config_values={
                    "shared_conda_target": "tools",
                    "autohic_conda_target": "autohic",
                    "autohic_root": str(package_roots["autohic"]),
                    "juicer_root": str(package_roots["juicer"]),
                    "three_d_dna_root": str(package_roots["three_d_dna"]),
                    "juicer_tools_root": str(package_roots["juicer_tools"]),
                    "error_model_path": str(error_model),
                    "chromosome_model_path": str(chromosome_model),
                    **database_values,
                },
            )
            apply_project_bootstrap_plan(plan)
            before = config_path.read_bytes()

            stderr = io.StringIO()
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch(
                    "builtins.input",
                    side_effect=["4", str(root / "missing-autohic")],
                ),
                contextlib.redirect_stderr(stderr),
            ):
                code = _run_bootstrap(
                    argparse.Namespace(
                        config=str(config_path),
                        update=True,
                    )
                )

            self.assertEqual(code, 1)
            self.assertEqual(config_path.read_bytes(), before)
            self.assertIn(str(root / "missing-autohic"), stderr.getvalue())

    def test_dotenv_only_update_does_not_repair_unselected_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: tools\n"
                "methods:\n  custom.method:\n    conda_env: your_tool_env\n"
                "packages: {}\n",
                encoding="utf-8",
            )
            _attach_database_registry(config_path, root)
            before = config_path.read_bytes()
            previous_key = os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
            try:
                with (
                    patch("sys.stdin.isatty", return_value=True),
                    patch("builtins.input", side_effect=["3", ""]),
                    patch("getpass.getpass", return_value="new-secret"),
                ):
                    code = _run_bootstrap(
                        argparse.Namespace(
                            config=str(config_path),
                            update=True,
                        )
                    )

                self.assertEqual(code, 1)
                self.assertEqual(config_path.read_bytes(), before)
                self.assertIn(
                    "GENE_AGENT_LLM_API_KEY=new-secret",
                    (root / ".env").read_text(encoding="utf-8"),
                )
            finally:
                if previous_key is None:
                    os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
                else:
                    os.environ["GENE_AGENT_LLM_API_KEY"] = previous_key

    def test_dotenv_overwrite_requires_field_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: tools\n", encoding="utf-8"
            )
            _attach_database_registry(config_path, root)
            dotenv_path = root / ".env"
            dotenv_path.write_text(
                "GENE_AGENT_LLM_API_KEY=old\nKEEP=yes\n", encoding="utf-8"
            )

            preserved = build_project_bootstrap_plan(
                config_path,
                dotenv_values={"GENE_AGENT_LLM_API_KEY": "new"},
                configure_autohic=False,
            )
            self.assertEqual(preserved.dotenv_updates, {})

            approved = build_project_bootstrap_plan(
                config_path,
                dotenv_values={"GENE_AGENT_LLM_API_KEY": "new"},
                approved_overwrites={".env.GENE_AGENT_LLM_API_KEY"},
                configure_autohic=False,
            )
            apply_project_bootstrap_plan(approved)
            self.assertEqual(
                dotenv_path.read_text(encoding="utf-8"),
                "GENE_AGENT_LLM_API_KEY=new\nKEEP=yes\n",
            )

    def test_apply_revalidates_dotenv_key_and_value_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: tools\npackages: {}\n",
                encoding="utf-8",
            )
            plan = build_project_bootstrap_plan(
                config_path,
                dotenv_values={"GENE_AGENT_LLM_MODEL": "valid"},
                approved_overwrites={".env.GENE_AGENT_LLM_MODEL"},
                configure_autohic=False,
            )
            plan.dotenv_updates["PATH"] = "bad\nvalue"
            plan.changes.append(
                ProjectConfigChange(plan.dotenv_path, "PATH", "bad\nvalue")
            )

            with self.assertRaisesRegex(ValueError, "bootstrap only writes"):
                apply_project_bootstrap_plan(plan)

            self.assertFalse(plan.dotenv_path.exists())

    def test_apply_rejects_relaxed_derived_conformance_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            package_roots, error_model, chromosome_model = _autohic_fixture(root)
            database_values = _database_values(root)
            plan = build_project_bootstrap_plan(
                config_path,
                config_values={
                    "shared_conda_target": "tools",
                    "autohic_conda_target": "autohic",
                    "autohic_root": str(package_roots["autohic"]),
                    "juicer_root": str(package_roots["juicer"]),
                    "three_d_dna_root": str(package_roots["three_d_dna"]),
                    "juicer_tools_root": str(package_roots["juicer_tools"]),
                    "error_model_path": str(error_model),
                    "chromosome_model_path": str(chromosome_model),
                    **database_values,
                },
            )
            self.assertEqual(plan.conformance_packages, {"juicer"})
            plan.conformance_packages.clear()

            with self.assertRaisesRegex(ValueError, "conformance_packages"):
                apply_project_bootstrap_plan(plan)

            self.assertFalse(config_path.exists())

    def test_autohic_paths_and_models_round_trip_through_env_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            package_roots, error_model, chromosome_model = _autohic_fixture(root)
            database_values = _database_values(root)
            roots = {
                "autohic_root": package_roots["autohic"],
                "juicer_root": package_roots["juicer"],
                "three_d_dna_root": package_roots["three_d_dna"],
                "juicer_tools_root": package_roots["juicer_tools"],
            }
            values = {key: str(path) for key, path in roots.items()}
            values.update(
                {
                    "shared_conda_target": "tools",
                    "autohic_conda_target": "autohic",
                    "error_model_path": str(error_model),
                    "chromosome_model_path": str(chromosome_model),
                    **database_values,
                }
            )
            plan = build_project_bootstrap_plan(
                config_path,
                config_values=values,
            )
            self.assertEqual(plan.unresolved_fields, [])

            resolver = apply_project_bootstrap_plan(plan)
            autohic = resolver.packages["autohic"]
            self.assertEqual(autohic.model_path("error"), error_model)
            self.assertEqual(autohic.model_path("chromosome"), chromosome_model)
            self.assertEqual(
                verify_envs_yaml(config_path, conformance_packages={"juicer"}), []
            )

    def test_custom_config_keeps_dotenv_next_to_custom_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "custom-envs.yaml"
            plan = build_project_bootstrap_plan(
                config_path,
                config_values={
                    "shared_conda_target": "tools",
                    **_database_values(root),
                },
                dotenv_values={"GENE_AGENT_LLM_MODEL": "custom-model"},
                configure_autohic=False,
                template_path=root / "missing-template.yaml",
            )
            apply_project_bootstrap_plan(plan)

            self.assertEqual(plan.dotenv_path, root / ".env")
            self.assertTrue((root / ".env").is_file())
            previous = os.environ.pop("GENE_AGENT_LLM_MODEL", None)
            try:
                _load_dotenv_without_override([root / ".env"])
                self.assertEqual(os.environ["GENE_AGENT_LLM_MODEL"], "custom-model")
            finally:
                if previous is None:
                    os.environ.pop("GENE_AGENT_LLM_MODEL", None)
                else:
                    os.environ["GENE_AGENT_LLM_MODEL"] = previous

    def test_verify_envs_yaml_rejects_database_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "envs.yaml"
            issues = verify_envs_yaml(
                config_path,
                config_data={
                    "default": {"fallback_env": "tools"},
                    "packages": {},
                    "stages": {},
                    "methods": {},
                    "databases": {
                        "busco": {
                            "default_lineage": "metazoa_odb10",
                            "lineages": {
                                "metazoa_odb10": {
                                    "path": "/path/to/busco/metazoa_odb10",
                                    "version": "odb10",
                                    "source": "busco_downloads",
                                }
                            },
                        },
                        "ncbi": {"reference_dir": "/path/to/databases/ncbi"},
                    },
                },
            )

        self.assertTrue(
            any(
                "databases.busco.lineages.metazoa_odb10.path" in issue
                for issue in issues
            )
        )
        self.assertTrue(
            any("databases.ncbi.reference_dir" in issue for issue in issues)
        )

    def test_verify_envs_yaml_deep_validates_database_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _database_fixture(root)
            (paths["busco"] / "scores_cutoff").write_text(
                "wrong_profile 0.5\n", encoding="utf-8"
            )
            issues = verify_envs_yaml(
                root / "envs.yaml",
                config_data={
                    "default": {"fallback_env": "tools"},
                    "packages": {},
                    "stages": {},
                    "methods": {},
                    "databases": {
                        "busco": {
                            "default_lineage": "metazoa_odb10",
                            "lineages": {
                                "metazoa_odb10": {
                                    "path": str(paths["busco"]),
                                    "version": "odb10",
                                    "source": "busco_downloads",
                                }
                            },
                        },
                        "ncbi": {
                            "reference_dir": str(paths["ncbi"]),
                        },
                    },
                },
            )

        self.assertTrue(any("scores_cutoff profiles" in issue for issue in issues))


def _autohic_fixture(root: Path) -> tuple[dict[str, Path], Path, Path]:
    """Create the package/model paths consumed by bootstrap preflight."""

    package_roots = {
        "autohic": root / "AutoHiC",
        "juicer": root / "juicer",
        "three_d_dna": root / "3d-dna",
        "juicer_tools": root / "juicer_tools",
    }
    files = [
        package_roots["autohic"] / "autohic.py",
        package_roots["autohic"] / "src/common/run.sh",
        package_roots["autohic"] / "src/models/cfgs/error_model.py",
        package_roots["juicer"] / "CPU/juicer.sh",
        package_roots["juicer"] / "CPU/common/chimeric_blacklist.awk",
        package_roots["juicer"] / "misc/generate_site_positions.py",
        package_roots["three_d_dna"] / "run-asm-pipeline.sh",
        package_roots["three_d_dna"] / "run-asm-pipeline-post-review.sh",
        package_roots["three_d_dna"] / "edit/apply-edits-prep-for-next-round.sh",
        package_roots["three_d_dna"] / "visualize/run-assembly-visualizer.sh",
        package_roots["juicer_tools"] / "juicer_tools.jar",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    error_model = package_roots["autohic"] / "models/error.pth"
    chromosome_model = package_roots["autohic"] / "models/chromosome.pth"
    error_model.parent.mkdir(parents=True, exist_ok=True)
    error_model.write_bytes(b"error")
    chromosome_model.write_bytes(b"chromosome")
    return package_roots, error_model, chromosome_model


def _database_fixture(root: Path) -> dict[str, Path]:
    """Create the smallest structurally valid BUSCO and NCBI fixtures."""

    busco = root / "metazoa_odb10"
    (busco / "hmms").mkdir(parents=True, exist_ok=True)
    (busco / "dataset.cfg").write_text(
        "name=metazoa_odb10\n"
        "domain=metazoa\n"
        "creation_date=2024-01-01\n"
        "number_of_BUSCOs=1\n"
        "number_of_species=1\n",
        encoding="utf-8",
    )
    (busco / "hmms" / "BUSCO1.hmm").write_text(
        "HMMER3/f [3.4 | Nov 2023]\n"
        "NAME  BUSCO1\n"
        "LENG  10\n"
        "ALPH  amino\n"
        "HMM  A C D E F G H I K L M N P Q R S T V W Y\n"
        "//\n",
        encoding="ascii",
    )
    (busco / "scores_cutoff").write_text("BUSCO1 0.5\n", encoding="utf-8")
    (busco / "lengths_cutoff").write_text("BUSCO1 10\n", encoding="utf-8")
    (busco / "refseq_db.faa.gz").write_bytes(b"fixture")

    ncbi_metazoa = _ncbi_fixture(root / "ncbi" / "metazoa", "Metazoa")
    ncbi_viridiplantae = _ncbi_fixture(root / "ncbi" / "viridiplantae", "Viridiplantae")
    return {
        "busco": busco,
        "ncbi": root / "ncbi",
        "ncbi_metazoa": ncbi_metazoa,
        "ncbi_viridiplantae": ncbi_viridiplantae,
    }


def _ncbi_fixture(root: Path, prefix: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    files = {
        f"{prefix}.ncbi.tsv": "Organism Name\tAssembly Accession\n",
        f"{prefix}.ncbi.lineage": "Organism Taxonomic ID\tLineage\n",
        "species_alias.tsv": "alias\tscientific_name\n",
    }
    manifest_files: dict[str, dict[str, int | str]] = {}
    for name, content in files.items():
        path = root / name
        path.write_text(content, encoding="utf-8")
        raw = path.read_bytes()
        manifest_files[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    (root / "reference_manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": "fixture",
                "source_provider": "tests",
                "source_version": "1",
                "exported_at": "2024-01-01T00:00:00Z",
                "files": manifest_files,
            }
        ),
        encoding="utf-8",
    )
    return root


def _database_values(root: Path) -> dict[str, str]:
    paths = _database_fixture(root)
    return {
        "busco_lineage_path": str(paths["busco"]),
        "ncbi_reference_dir": str(paths["ncbi"]),
    }


def _minimal_host_config(root: Path) -> dict[str, object]:
    paths = _database_fixture(root)
    lineage = paths["busco"]
    return {
        "default": {"fallback_env": "tools"},
        "packages": {},
        "databases": {
            "busco": {
                "default_lineage": lineage.name,
                "lineages": {
                    lineage.name: {
                        "path": str(lineage),
                        "version": "odb10",
                        "source": "tests",
                    }
                },
            },
            "ncbi": {
                "reference_dir": str(paths["ncbi"]),
            },
        },
    }


def _attach_database_registry(config_path: Path, root: Path) -> None:
    document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    document["databases"] = _minimal_host_config(root)["databases"]
    config_path.write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
