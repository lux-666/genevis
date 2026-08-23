"""Tests for Phase 4 env resolver, preflight, and LocalRunner conda wrapping."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.execution import env_resolver
from app.execution.env_resolver import (
    EnvResolver,
    EnvSpec,
    preflight,
)
from app.execution.local_runner import LocalRunner
from app.integrity import sha256_file
from app.schemas.descriptors import FrozenAttemptSpec


class EnvResolverPrecedenceTests(unittest.TestCase):
    def test_default_active_config_is_cwd_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("GENE_AGENT_ENVS_CONFIG", None)
                    active = env_resolver.active_config_path()
            finally:
                os.chdir(previous)

        self.assertEqual(active, env_resolver.DEFAULT_CONFIG_PATH)
        self.assertTrue(active.is_absolute())

    def test_relative_active_config_override_uses_caller_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict(
                    os.environ,
                    {"GENE_AGENT_ENVS_CONFIG": "host/envs.yaml"},
                ):
                    active = env_resolver.active_config_path()
            finally:
                os.chdir(previous)

        self.assertEqual(active, Path(tmp).resolve() / "host" / "envs.yaml")

    def test_implicit_resolver_uses_default_config_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "envs.yaml"
            config.write_text(
                "default:\n  fallback_env: shared_env\n", encoding="utf-8"
            )
            with patch.object(env_resolver, "DEFAULT_CONFIG_PATH", config):
                resolver = EnvResolver()

            self.assertEqual(resolver.config_path, config)
            self.assertEqual(resolver.resolve(stage="unknown").conda_env, "shared_env")

    def test_env_template_loads_and_covers_core_mainline(self):
        template_path = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "examples"
            / "envs.yaml.example"
        )
        resolver = EnvResolver(template_path)

        seqkit_spec = resolver.resolve(
            stage="read_qc",
            method_name="read_qc.long_read_stats.seqkit_stats",
        )
        self.assertTrue(seqkit_spec.conda_env or seqkit_spec.conda_prefix)
        self.assertEqual(seqkit_spec.required_tools, ["seqkit"])
        self.assertEqual(seqkit_spec.source, "method")

        flye_spec = resolver.resolve(
            stage="assembly",
            method_name="assembly.candidate.flye",
        )
        self.assertTrue(flye_spec.conda_env or flye_spec.conda_prefix)
        self.assertEqual(flye_spec.required_tools, ["flye"])

        quast_spec = resolver.resolve(
            stage="assembly_review",
            method_name="assembly_review.eval.quast",
        )
        self.assertTrue(quast_spec.conda_env or quast_spec.conda_prefix)
        self.assertEqual(quast_spec.required_tools, ["quast.py"])

        busco_spec = resolver.resolve(
            stage="assembly_review",
            method_name="assembly_review.eval.busco",
        )
        self.assertTrue(busco_spec.conda_env or busco_spec.conda_prefix)
        self.assertEqual(busco_spec.required_tools, ["busco"])

        autohic_spec = resolver.resolve(
            stage="hic_scaffolding",
            method_name="hic_scaffolding.candidate.autohic",
        )
        self.assertTrue(autohic_spec.conda_env or autohic_spec.conda_prefix)
        self.assertIn("python3.9", autohic_spec.required_tools)
        self.assertIn("java", autohic_spec.required_tools)
        self.assertEqual(
            autohic_spec.package_tools, ["autohic", "juicer", "three_d_dna"]
        )
        packages = resolver.packages_for_spec(autohic_spec)
        self.assertEqual(
            {pkg.name for pkg in packages}, {"autohic", "juicer", "three_d_dna"}
        )
        self.assertTrue(all(pkg.install_root for pkg in packages))

    def test_typed_database_registry_resolves_default_and_named_busco_lineages(self):
        resolver = EnvResolver(
            config_data={
                "default": {"fallback_env": "base"},
                "databases": {
                    "busco": {
                        "default_lineage": "metazoa_odb10",
                        "lineages": {
                            "metazoa_odb10": {
                                "path": "/db/busco/metazoa_odb10",
                                "version": "odb10",
                                "source": "busco_downloads",
                            },
                            "embryophyta_odb10": {
                                "path": "/db/busco/embryophyta_odb10",
                                "version": "odb10",
                                "source": "busco_downloads",
                            },
                        },
                    },
                    "ncbi_metazoa": {"reference_dir": "/db/ncbi_metazoa"},
                },
            }
        )

        self.assertIsNotNone(resolver.databases)
        self.assertEqual(
            resolver.resolve_busco_lineage().path, "/db/busco/metazoa_odb10"
        )
        self.assertEqual(
            resolver.resolve_busco_lineage("embryophyta_odb10").version, "odb10"
        )
        self.assertEqual(
            resolver.databases.ncbi_metazoa.reference_dir, "/db/ncbi_metazoa"
        )

    def test_database_registry_rejects_unknown_empty_and_missing_default_values(self):
        base = {
            "default": {"fallback_env": "base"},
            "databases": {
                "busco": {
                    "default_lineage": "metazoa_odb10",
                    "lineages": {
                        "metazoa_odb10": {
                            "path": "/db/busco/metazoa_odb10",
                            "version": "odb10",
                            "source": "downloads",
                        }
                    },
                },
                "ncbi_metazoa": {"reference_dir": "/db/ncbi_metazoa"},
            },
        }
        cases = (
            ("unknown key", {"databases": {"extra": {}}}),
            (
                "empty path",
                {
                    "databases": {
                        "busco": {
                            "default_lineage": "metazoa_odb10",
                            "lineages": {
                                "metazoa_odb10": {
                                    "path": "",
                                    "version": "odb10",
                                    "source": "downloads",
                                }
                            },
                        },
                        "ncbi_metazoa": {"reference_dir": "/db/ncbi_metazoa"},
                    }
                },
            ),
            (
                "missing default lineage",
                {
                    "databases": {
                        "busco": {
                            "default_lineage": "missing",
                            "lineages": base["databases"]["busco"]["lineages"],
                        },
                        "ncbi_metazoa": {"reference_dir": "/db/ncbi_metazoa"},
                    }
                },
            ),
        )
        for label, override in cases:
            with self.subTest(label=label):
                payload = {**base, **override}
                with self.assertRaisesRegex(ValueError, "databases"):
                    EnvResolver(config_data=payload)

    def test_busco_resolution_requires_a_host_registry(self):
        resolver = EnvResolver(config_data={"default": {"fallback_env": "base"}})
        with self.assertRaisesRegex(ValueError, "databases.busco"):
            resolver.resolve_busco_lineage()

    def test_method_override_beats_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "envs.yaml"
            path.write_text(
                "default:\n"
                "  conda_bin: conda\n"
                "  fallback_env: base\n"
                "stages:\n"
                "  assembly:\n"
                "    conda_env: env_assembly\n"
                "methods:\n"
                "  assembly.candidate.flye:\n"
                "    conda_env: env_flye_only\n"
                "    required_tools: [flye]\n",
                encoding="utf-8",
            )
            resolver = EnvResolver(path)
            spec = resolver.resolve(
                stage="assembly",
                method_name="assembly.candidate.flye",
            )
            self.assertEqual(spec.conda_env, "env_flye_only")
            self.assertEqual(spec.source, "method")

    def test_stage_beats_default_when_method_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "envs.yaml"
            path.write_text(
                "default:\n"
                "  conda_bin: conda\n"
                "  fallback_env: base\n"
                "stages:\n"
                "  read_qc:\n"
                "    conda_env: env_read_qc\n",
                encoding="utf-8",
            )
            resolver = EnvResolver(path)

            method_spec = resolver.resolve(
                stage="read_qc",
                method_name="read_qc.unknown.method",
            )
            self.assertEqual(method_spec.conda_env, "env_read_qc")
            self.assertEqual(method_spec.source, "stage")

            stage_only = resolver.resolve(stage="read_qc")
            self.assertEqual(stage_only.conda_env, "env_read_qc")
            self.assertEqual(stage_only.source, "stage")

    def test_default_fallback_when_stage_and_method_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "envs.yaml"
            path.write_text(
                "default:\n  conda_bin: conda\n  fallback_env: base_env\n",
                encoding="utf-8",
            )
            resolver = EnvResolver(path)
            spec = resolver.resolve(stage="unknown_stage", method_name=None)
            self.assertEqual(spec.conda_env, "base_env")
            self.assertEqual(spec.source, "default")
            self.assertEqual(spec.required_tools, [])

    def test_prefix_path_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "envs.yaml"
            path.write_text(
                "default:\n"
                "  conda_bin: conda\n"
                "  fallback_env: base\n"
                "methods:\n"
                "  read_qc.long_read_stats.seqkit_stats:\n"
                "    conda_prefix: /opt/envs/gene_agent\n"
                "    required_tools: [seqkit]\n",
                encoding="utf-8",
            )
            resolver = EnvResolver(path)
            spec = resolver.resolve(
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
            )
            self.assertIsNone(spec.conda_env)
            self.assertEqual(spec.conda_prefix, "/opt/envs/gene_agent")
            self.assertEqual(
                spec.conda_run_prefix(),
                [
                    "conda",
                    "run",
                    "--live-stream",
                    "--no-capture-output",
                    "-p",
                    "/opt/envs/gene_agent",
                ],
            )

    def test_env_and_prefix_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            EnvSpec(conda_env="x", conda_prefix="/y")

    def test_missing_config_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                EnvResolver(Path(tmp) / "missing.yaml")

    def test_missing_active_config_does_not_fallback_to_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_path = Path(tmp) / "config" / "envs.yaml"
            with patch.object(env_resolver, "DEFAULT_CONFIG_PATH", active_path):
                with self.assertRaises(FileNotFoundError):
                    EnvResolver()

    def test_malformed_yaml_is_normalized_to_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "envs.yaml"
            config.write_text("default: [unterminated\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "env config YAML is invalid"):
                EnvResolver(config)


class EnvPreflightTests(unittest.TestCase):
    def _spec(self) -> EnvSpec:
        return EnvSpec(
            conda_bin="conda-stub",
            conda_env="env_assembly_flye",
            required_tools=["flye"],
            source="method",
        )

    def test_preflight_blocked_env_when_conda_missing(self):
        spec = self._spec()
        os_path = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = ""
            result = preflight(
                spec,
                runner=lambda *a, **k: SimpleNamespace(
                    returncode=0, stdout="", stderr=""
                ),
            )
            self.assertEqual(result.status, "blocked_env")
            self.assertIn("conda", result.detail)
        finally:
            os.environ["PATH"] = os_path

    def test_preflight_blocked_env_when_conda_run_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            conda_stub = Path(tmp) / "conda-stub"
            conda_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            conda_stub.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{tmp}{os.pathsep}{old_path}"
            try:

                def fake_run(argv, **kwargs):
                    return SimpleNamespace(
                        returncode=1, stdout="", stderr="env not found"
                    )

                result = preflight(self._spec(), runner=fake_run)
                self.assertEqual(result.status, "blocked_env")
                self.assertIn("env not found", result.detail)
            finally:
                os.environ["PATH"] = old_path

    def test_preflight_missing_tool_surfaces_tool_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            conda_stub = Path(tmp) / "conda-stub"
            conda_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            conda_stub.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{tmp}{os.pathsep}{old_path}"
            try:
                calls = []

                def fake_run(argv, **kwargs):
                    calls.append(argv)
                    if argv[-1] == "true":
                        return SimpleNamespace(returncode=0, stdout="", stderr="")
                    return SimpleNamespace(returncode=1, stdout="", stderr="not found")

                result = preflight(self._spec(), runner=fake_run)
                self.assertEqual(result.status, "missing_tool")
                self.assertEqual(result.missing_tools, ["flye"])
                tool_probes = [c for c in calls if "sh" in c and "-c" in c]
                self.assertTrue(tool_probes, "preflight should issue a tool probe")
                probe = tool_probes[0]
                # Shell-safe form: tool name is a positional arg, not interpolated
                # into the script body.
                self.assertEqual(probe[-1], "flye")
                self.assertIn('command -v "$1"', probe)
                self.assertNotIn("command -v flye", " ".join(probe))
            finally:
                os.environ["PATH"] = old_path

    def test_preflight_probe_is_injection_safe_for_exotic_tool_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            conda_stub = Path(tmp) / "conda-stub"
            conda_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            conda_stub.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{tmp}{os.pathsep}{old_path}"
            try:
                probes: list[list[str]] = []

                def fake_run(argv, **kwargs):
                    probes.append(list(argv))
                    if argv[-1] == "true":
                        return SimpleNamespace(returncode=0, stdout="", stderr="")
                    return SimpleNamespace(returncode=1, stdout="", stderr="")

                hostile = "tool; rm -rf /"
                spec = EnvSpec(
                    conda_bin="conda-stub",
                    conda_env="env_hostile",
                    required_tools=[hostile],
                )
                result = preflight(spec, runner=fake_run)
                self.assertEqual(result.status, "missing_tool")
                tool_probe = probes[-1]
                # The hostile tool name lands as a positional argv entry and is
                # not embedded in the shell script body, so `sh -c` cannot
                # interpret the `;` or the `rm -rf /` as separate commands.
                self.assertEqual(tool_probe[-1], hostile)
                self.assertIn('command -v "$1"', tool_probe)
                self.assertNotIn(hostile, 'command -v "$1"')
            finally:
                os.environ["PATH"] = old_path

    def test_preflight_ok_when_env_and_tools_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            conda_stub = Path(tmp) / "conda-stub"
            conda_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            conda_stub.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{tmp}{os.pathsep}{old_path}"
            try:

                def fake_run(argv, **kwargs):
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                result = preflight(self._spec(), runner=fake_run)
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.missing_tools, [])
            finally:
                os.environ["PATH"] = old_path


class LocalRunnerCondaWrappingTests(unittest.TestCase):
    def _spec(
        self,
        tmp: Path,
        *,
        conda_bin: str,
        conda_env: str | None = None,
        conda_prefix: str | None = None,
    ) -> FrozenAttemptSpec:
        working = tmp / "work"
        working.mkdir(parents=True, exist_ok=True)
        output = working / "touched.txt"
        script = working / "run.sh"
        script.write_text(
            f"#!/bin/sh\necho hello > {output}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return FrozenAttemptSpec(
            attempt_id="t1",
            session_id="s1",
            stage="read_qc",
            method_name="read_qc.long_read_stats.seqkit_stats",
            script_path=script,
            script_sha256=sha256_file(script),
            working_dir=working,
            interpreter="/bin/bash",
            expected_outputs=[output],
            input_dependencies=[],
            conda_bin=conda_bin,
            conda_env=conda_env,
            conda_prefix=conda_prefix,
        )

    def test_runner_passes_conda_run_argv_when_env_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Stub conda that records its argv, then runs whatever bash command
            # it was handed (`bash run.sh`) in the working dir so the real
            # script still executes.
            log_file = tmp_path / "conda_argv.log"
            conda_stub = tmp_path / "conda"
            conda_stub.write_text(
                "#!/bin/sh\n"
                f'echo "$@" > {log_file}\n'
                'while [ "$#" -gt 0 ]; do\n'
                '  case "$1" in\n'
                "    run|--live-stream|--no-capture-output) shift ;;\n"
                "    -n|-p) shift 2 ;;\n"
                "    *) break ;;\n"
                "  esac\n"
                "done\n"
                'exec "$@"\n',
                encoding="utf-8",
            )
            conda_stub.chmod(0o755)

            spec = self._spec(
                tmp_path, conda_bin=str(conda_stub), conda_env="env_read_qc"
            )
            result = LocalRunner().run(spec)

            self.assertTrue(result.success)
            self.assertEqual(result.status, "ok")
            argv_line = log_file.read_text(encoding="utf-8").strip()
            self.assertIn("run", argv_line)
            self.assertIn("-n", argv_line)
            self.assertIn("env_read_qc", argv_line)
            self.assertIn(str(spec.script_path), argv_line)
            self.assertTrue((tmp_path / "work" / "touched.txt").exists())

    def test_runner_uses_prefix_flag_when_conda_prefix_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log_file = tmp_path / "conda_argv.log"
            conda_stub = tmp_path / "conda"
            conda_stub.write_text(
                "#!/bin/sh\n"
                f'echo "$@" > {log_file}\n'
                'while [ "$#" -gt 0 ]; do\n'
                '  case "$1" in\n'
                "    run|--live-stream|--no-capture-output) shift ;;\n"
                "    -n|-p) shift 2 ;;\n"
                "    *) break ;;\n"
                "  esac\n"
                "done\n"
                'exec "$@"\n',
                encoding="utf-8",
            )
            conda_stub.chmod(0o755)

            spec = self._spec(
                tmp_path, conda_bin=str(conda_stub), conda_prefix="/opt/envs/read_qc"
            )
            result = LocalRunner().run(spec)

            self.assertTrue(result.success)
            argv_line = log_file.read_text(encoding="utf-8").strip()
            self.assertIn("-p", argv_line)
            self.assertIn("/opt/envs/read_qc", argv_line)

    def test_runner_preserves_legacy_path_when_conda_env_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spec = self._spec(
                tmp_path, conda_bin="conda", conda_env=None, conda_prefix=None
            )
            # Without conda_env set, runner must not invoke conda. Remove conda
            # from PATH so the test fails loudly if LocalRunner tries.
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ""
            try:
                result = LocalRunner().run(spec)
            finally:
                os.environ["PATH"] = old_path

            self.assertTrue(result.success)
            self.assertEqual(result.status, "ok")
            self.assertTrue((tmp_path / "work" / "touched.txt").exists())

    def test_runner_failure_sets_failed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            working = Path(tmp) / "work"
            working.mkdir(parents=True, exist_ok=True)
            script = working / "run.sh"
            script.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
            script.chmod(0o755)
            spec = FrozenAttemptSpec(
                attempt_id="t2",
                session_id="s1",
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                script_path=script,
                script_sha256=sha256_file(script),
                working_dir=working,
                interpreter="/bin/bash",
                expected_outputs=[],
                input_dependencies=[],
            )
            result = LocalRunner().run(spec)
            self.assertFalse(result.success)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.return_code, 3)

    def test_frozen_attempt_spec_rejects_env_and_prefix_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            working = Path(tmp) / "work"
            working.mkdir(parents=True, exist_ok=True)
            script = working / "run.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                FrozenAttemptSpec(
                    attempt_id="t3",
                    session_id="s1",
                    stage="read_qc",
                    method_name="read_qc.long_read_stats.seqkit_stats",
                    script_path=script,
                    script_sha256=sha256_file(script),
                    working_dir=working,
                    interpreter="/bin/bash",
                    expected_outputs=[],
                    input_dependencies=[],
                    conda_env="env_read_qc",
                    conda_prefix="/opt/envs/read_qc",
                )


if __name__ == "__main__":
    unittest.main()
