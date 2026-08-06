"""Tests for bootstrap-time method environment preflight."""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.entrypoints.run import _run_bootstrap
from app.execution.env_resolver import EnvResolver
from app.execution.method_preflight import preflight_methods


class MethodPreflightTests(unittest.TestCase):
    def _resolver(self, root: Path, conda_one: Path, conda_two: Path) -> EnvResolver:
        config = root / "envs.yaml"
        config.write_text(
            "default:\n"
            f"  conda_bin: {conda_one}\n"
            "  fallback_env: shared\n"
            "methods:\n"
            "  read_qc.seqkit:\n"
            "    conda_env: shared\n"
            "    required_tools: [seqkit]\n"
            "  assembly.flye:\n"
            "    conda_env: shared\n"
            "    required_tools: [seqkit, flye]\n"
            "  assembly.busco:\n"
            f"    conda_bin: {conda_two}\n"
            "    conda_env: review\n"
            "    required_tools: [busco]\n"
            "packages: {}\n",
            encoding="utf-8",
        )
        return EnvResolver(config)

    def test_groups_same_environment_and_maps_missing_tools_to_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conda_one = root / "conda-one"
            conda_two = root / "conda-two"
            for binary in (conda_one, conda_two):
                binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binary.chmod(0o755)
            resolver = self._resolver(root, conda_one, conda_two)
            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[-1] == "true":
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(
                    returncode=0 if argv[-1] == "seqkit" else 1,
                    stdout="",
                    stderr="missing",
                )

            results = preflight_methods(resolver, runner=fake_run)
            by_method = {result.method_name: result for result in results}

            self.assertEqual(by_method["read_qc.seqkit"].status, "ok")
            self.assertEqual(
                by_method["assembly.flye"].missing_tools,
                ["flye"],
            )
            self.assertEqual(
                by_method["assembly.busco"].missing_tools,
                ["busco"],
            )
            tool_probes = [call for call in calls if "-c" in call]
            self.assertEqual(
                [call[-1] for call in tool_probes],
                ["seqkit", "flye", "busco"],
            )
            self.assertEqual(
                sum(call[-1] == "true" for call in calls),
                2,
            )

    def test_probe_oserror_is_reported_as_blocked_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conda = root / "conda"
            conda.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            conda.chmod(0o755)
            resolver = self._resolver(root, conda, conda)

            def failing_run(argv, **kwargs):
                raise OSError("container runtime unavailable")

            results = preflight_methods(resolver, runner=failing_run)

            self.assertTrue(results)
            self.assertTrue(all(result.status == "blocked_env" for result in results))
            self.assertIn("container runtime unavailable", results[0].detail)

    def test_bootstrap_reports_method_and_missing_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conda = root / "conda"
            conda.write_text(
                "#!/bin/sh\n"
                "last=''\n"
                "for arg in \"$@\"; do last=\"$arg\"; done\n"
                "[ \"$last\" = flye ] && exit 1\n"
                "exit 0\n",
                encoding="utf-8",
            )
            conda.chmod(0o755)
            config = root / "envs.yaml"
            config.write_text(
                "default:\n"
                f"  conda_bin: {conda}\n"
                "  fallback_env: tools\n"
                "methods:\n"
                "  assembly.candidate.flye:\n"
                "    required_tools: [flye]\n"
                "packages: {}\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = _run_bootstrap(
                    argparse.Namespace(config=str(config), apply=True)
                )

            self.assertEqual(code, 1)
            output = stdout.getvalue()
            self.assertIn("assembly.candidate.flye", output)
            self.assertIn("[tools]", output)
            self.assertIn("missing tool(s): flye", output)
            self.assertIn("Fix: install the listed executable", output)

    def test_bootstrap_reports_missing_package_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "envs.yaml"
            missing_root = root / "missing-autohic"
            config.write_text(
                "default:\n  fallback_env: tools\n"
                "packages:\n"
                "  autohic:\n"
                f"    install_root: {missing_root}\n"
                "    canary_paths: [autohic.py]\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = _run_bootstrap(
                    argparse.Namespace(config=str(config), apply=True)
                )

            self.assertEqual(code, 1)
            output = stdout.getvalue()
            self.assertIn("packages.autohic.install_root does not exist", output)
            self.assertIn("autohic: install_root does not exist", output)
            self.assertIn("missing: <install_root>", output)


if __name__ == "__main__":
    unittest.main()
