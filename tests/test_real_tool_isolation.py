"""Default and explicit real-tool collection must not execute tool probes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


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
    env = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
        "HIFIASM_BIN": str(binaries / "hifiasm"),
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
