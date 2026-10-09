"""Default and explicit real-tool collection must not execute tool probes."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


def _registry_conda(tmp_path: Path) -> Path:
    conda = tmp_path / "conda"
    conda.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        'case "$1" in run|--live-stream|--no-capture-output) shift;;\n'
        '-p) target="$2"; shift 2;; -n) exit 7;; *) break;; esac\ndone\n'
        'printf "%s %s\\n" "$target" "$*" >> '
        f"{shlex.quote(str(tmp_path / 'calls.log'))}\n"
        'export PATH="$target/bin:/usr/bin:/bin"\n'
        'exec "$@"\n'
    )
    conda.chmod(0o755)
    return conda


def test_read_qc_real_smoke_uses_active_registry(tmp_path: Path, monkeypatch):
    from tests.test_read_qc import Phase4ReadQCRealSmokeTests, ReadQCSeqKitStageTests

    prefix = tmp_path / "registered-seqkit"
    prefix.mkdir()
    ReadQCSeqKitStageTests()._fake_seqkit_bin(str(prefix))
    # This smoke fixture has three reads, matching the fake stdin aggregate.
    seqkit = prefix / "bin" / "seqkit"
    seqkit.write_text(
        seqkit.read_text().replace("reads=1; bases=4", "reads=3; bases=12")
    )
    conda = _registry_conda(tmp_path)
    registry = tmp_path / "host-registry.yaml"
    registry.write_text(
        json.dumps(
            {
                "default": {"conda_bin": str(conda), "fallback_env": "wrong-fallback"},
                "methods": {
                    "read_qc.long_read_stats.seqkit_stats": {
                        "conda_prefix": str(prefix),
                        "required_tools": ["seqkit"],
                    }
                },
            }
        )
    )
    monkeypatch.setenv("GENE_AGENT_ENVS_CONFIG", str(registry))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    smoke = Phase4ReadQCRealSmokeTests()
    smoke.test_generated_fixture_smoke_produces_stats_summary_and_fofn()
    calls = (tmp_path / "calls.log").read_text()
    assert "bash" in calls
    assert all(line.startswith(str(prefix) + " ") for line in calls.splitlines())


def test_hifiasm_cli_check_uses_each_registered_method(tmp_path: Path, monkeypatch):
    from tests.test_phase10_parameter_catalog import (
        ParameterCatalogSchemaTests,
        _project_hifiasm_min_version,
        load_parameter_catalog,
    )

    conda = _registry_conda(tmp_path)
    methods = {}
    version = ".".join(str(part) for part in _project_hifiasm_min_version())
    for name in ("hifiasm", "hifiasm_ont"):
        method_id = f"assembly.candidate.{name}"
        prefix = tmp_path / name
        binary = prefix / "bin" / "hifiasm"
        binary.parent.mkdir(parents=True)
        flags = {"--ont", "--rl-cut", "--sc-cut"}
        for parameter in load_parameter_catalog(method_id).parameters.values():
            application = parameter.application
            if getattr(application, "flag", None):
                flags.add(application.flag)
            flags.update(getattr(application, "flags", {}).values())
        help_text = "\n".join("  " + flag for flag in sorted(flags))
        binary.write_text(
            "#!/bin/sh\n"
            f'case "$1" in --version) echo {version};;\n'
            f"-h) printf '%s\\n' {shlex.quote(help_text)};; *) exit 2;; esac\n"
        )
        binary.chmod(0o755)
        methods[method_id] = {
            "conda_prefix": str(prefix),
            "required_tools": ["hifiasm"],
        }
    registry = tmp_path / "host-registry.yaml"
    registry.write_text(
        json.dumps(
            {
                "default": {"conda_bin": str(conda), "fallback_env": "wrong-fallback"},
                "methods": methods,
            }
        )
    )
    monkeypatch.setenv("GENE_AGENT_ENVS_CONFIG", str(registry))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HIFIASM_BIN", "/invalid/legacy-hifiasm")
    smoke = ParameterCatalogSchemaTests()
    smoke.test_cataloged_hifiasm_flags_exist_in_runtime_help_when_binary_available()
    calls = (tmp_path / "calls.log").read_text().splitlines()
    for mapping in methods.values():
        prefix = mapping["conda_prefix"]
        assert f"{prefix} hifiasm --version" in calls
        assert f"{prefix} hifiasm -h" in calls


@pytest.mark.parametrize("explicit", [False, True])
def test_real_tool_selection_never_probes_during_collection(tmp_path: Path, explicit):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    probe_log = tmp_path / "probes.log"
    for name in ("conda", "hifiasm", "minimap2", "racon", "tgsgapcloser"):
        binary = binaries / name
        binary.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0" >> "$GENE_AGENT_TOOL_PROBE_LOG"\nexit 1\n'
        )
        binary.chmod(0o755)
    registry = tmp_path / "collection-registry.yaml"
    registry.write_text(
        json.dumps(
            {
                "default": {
                    "conda_bin": str(binaries / "conda"),
                    "fallback_env": "collection-must-not-probe",
                }
            }
        )
    )
    env = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
        "GENE_AGENT_ENVS_CONFIG": str(registry),
        "GENE_AGENT_TOOL_PROBE_LOG": str(probe_log),
    }
    env.pop("PYTEST_ADDOPTS", None)
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    if explicit:
        command.extend(["-m", "real_tool"])
    command.extend(
        [
            "tests/test_read_qc.py",
            "tests/test_base_polish.py",
            "tests/test_gapcloser.py",
            "tests/test_phase10_parameter_catalog.py",
        ]
    )
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not probe_log.exists(), "collection invoked an installed tool"
    for test_name in (
        "Phase4ReadQCRealSmokeTests::test_generated_fixture_smoke",
        "test_real_racon_hifi_smoke",
        "test_real_tgs_gapcloser_hifi_smoke",
        "test_cataloged_hifiasm_flags_exist_in_runtime_help",
    ):
        assert (test_name in result.stdout) == explicit
