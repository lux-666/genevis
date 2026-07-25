"""Focused tests for minimal project bootstrap initialization."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.entrypoints.run import _load_dotenv_without_override, _run_bootstrap
from app.execution.env_resolver import EnvResolver
from app.execution.package_bootstrap import verify_envs_yaml
from app.execution.project_bootstrap import (
    apply_project_bootstrap_plan,
    build_project_bootstrap_plan,
)


class ProjectBootstrapTests(unittest.TestCase):
    def test_cli_creates_consumable_autohic_config_and_redacts_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            package_roots, error_model, chromosome_model = _autohic_fixture(root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            previous_key = os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
            try:
                answers = [
                    "tools",
                    str(package_roots["autohic"]),
                    str(package_roots["juicer"]),
                    str(package_roots["three_d_dna"]),
                    str(package_roots["juicer_tools"]),
                    str(error_model),
                    str(chromosome_model),
                    "y",
                    "",
                    "",
                ]
                with (
                    patch("builtins.input", side_effect=answers),
                    patch("getpass.getpass", return_value="sk-bootstrap-secret"),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = _run_bootstrap(
                        argparse.Namespace(config=str(config_path), apply=True)
                    )

                self.assertEqual(code, 0, stderr.getvalue())
                self.assertTrue(config_path.is_file())
                resolver = EnvResolver(config_path)
                self.assertEqual(resolver.resolve(stage="unknown").conda_env, "tools")
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
                self.assertEqual(
                    os.environ["GENE_AGENT_LLM_API_KEY"], "sk-bootstrap-secret"
                )
            finally:
                if previous_key is None:
                    os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
                else:
                    os.environ["GENE_AGENT_LLM_API_KEY"] = previous_key

    def test_rerun_is_idempotent_and_does_not_prompt_for_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: tools\npackages: {}\n", encoding="utf-8"
            )
            before = config_path.read_bytes()

            with patch(
                "builtins.input",
                side_effect=AssertionError("valid config must not prompt"),
            ):
                code = _run_bootstrap(
                    argparse.Namespace(config=str(config_path), apply=True)
                )

            self.assertEqual(code, 0)
            self.assertEqual(config_path.read_bytes(), before)

    def test_interactive_bootstrap_can_add_dotenv_to_existing_valid_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: tools\npackages: {}\n", encoding="utf-8"
            )
            previous_key = os.environ.pop("GENE_AGENT_LLM_API_KEY", None)
            try:
                # no AutoHiC package setup, create .env, no URL/model overrides
                with (
                    patch("sys.stdin.isatty", return_value=True),
                    patch("builtins.input", side_effect=["n", "y", "", ""]),
                    patch("getpass.getpass", return_value="sk-existing-project"),
                ):
                    code = _run_bootstrap(
                        argparse.Namespace(config=str(config_path), apply=True)
                    )

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
            config_path = Path(tmp) / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  conda_bin: micromamba\n"
                "  fallback_env: existing\ncustom:\n  keep: value\n",
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

    def test_dotenv_overwrite_requires_field_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "default:\n  fallback_env: tools\n", encoding="utf-8"
            )
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

    def test_autohic_paths_and_models_round_trip_through_env_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "envs.yaml"
            roots = {
                "autohic_root": root / "AutoHiC",
                "juicer_root": root / "juicer",
                "three_d_dna_root": root / "3d-dna",
                "juicer_tools_root": root / "juicer_tools",
            }
            for path in roots.values():
                path.mkdir(parents=True)
            model_dir = roots["autohic_root"] / "models"
            model_dir.mkdir()
            (model_dir / "error.pth").write_bytes(b"error")
            (model_dir / "chromosome.pth").write_bytes(b"chromosome")

            values = {key: str(path) for key, path in roots.items()}
            values.update(
                {
                    "shared_conda_target": "tools",
                    "autohic_conda_target": "autohic",
                    "error_model_path": "models/error.pth",
                    "chromosome_model_path": "models/chromosome.pth",
                }
            )
            plan = build_project_bootstrap_plan(
                config_path,
                config_values=values,
            )
            self.assertEqual(plan.unresolved_fields, [])

            resolver = apply_project_bootstrap_plan(plan)
            autohic = resolver.packages["autohic"]
            self.assertEqual(autohic.model_path("error"), model_dir / "error.pth")
            self.assertEqual(
                autohic.model_path("chromosome"), model_dir / "chromosome.pth"
            )
            self.assertEqual(verify_envs_yaml(config_path), [])

    def test_custom_config_keeps_dotenv_next_to_custom_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "custom-envs.yaml"
            plan = build_project_bootstrap_plan(
                config_path,
                config_values={"shared_conda_target": "tools"},
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


if __name__ == "__main__":
    unittest.main()
