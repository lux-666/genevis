"""Paired QC must preflight and freeze every method selected by its run plan."""

import json
import subprocess

import pytest

from app.execution.dependency_snapshot import load_frozen_dependency_resolver
from app.execution.run_readiness import (
    RunReadinessError,
    effective_method_closure,
    preflight_run_readiness,
)
from app.schemas.run_config import RunConfig
from app.storage.frozen_inputs import freeze_session_inputs
from app.storage.paths import SessionPaths


def _config():
    return RunConfig.model_validate(
        {
            "requested_stages": [{"stage": "read_qc", "allow_local_execution": True}],
            "read_qc": {"short_reads": {}, "hic_reads": {}},
        }
    )


def _envs(tmp_path):
    path = tmp_path / "envs.yaml"
    path.write_text(
        json.dumps(
            {
                "default": {"fallback_env": "qc-tools"},
                "stages": {"read_qc": {"required_tools": ["seqkit"]}},
                "methods": {
                    f"read_qc.{family}.fastp": {"required_tools": ["fastp"]}
                    for family in ("short_reads", "hic_reads")
                },
            }
        )
    )
    return path


def test_both_fastp_methods_resolve_from_newly_frozen_inputs(tmp_path):
    assets = []
    for family, asset_type in (
        ("short_reads", "raw_short_dna_reads"),
        ("hic_reads", "hic_reads_registered"),
    ):
        r1, r2 = (tmp_path / f"{family}_{mate}.fq" for mate in (1, 2))
        for path in (r1, r2):
            path.write_text("@r\nACGT\n+\nIIII\n")
        assets.append(
            {
                "asset_type": asset_type,
                "paired_reads": [{"r1": str(r1), "r2": str(r2)}],
                "platform": "illumina",
                "format": "fastq",
                "compression": "none",
            }
        )
    config = _config()
    paths = SessionPaths(tmp_path / "data", "paired-qc")
    freeze_session_inputs(
        paths,
        manifest_draft={"session_id": paths.session_id, "assets": assets},
        run_config_draft=config,
        config_path=_envs(tmp_path),
        probe_environments=False,
    )
    resolver = load_frozen_dependency_resolver(paths.run_dir)
    for family in ("short_reads", "hic_reads"):
        spec = resolver.resolve(stage="read_qc", method_name=f"read_qc.{family}.fastp")
        assert spec.conda_env == "qc-tools"
        assert spec.required_tools == ["fastp"]


def test_missing_fastp_is_rejected_before_freezing(tmp_path):
    def runner(command, **_kwargs):
        missing = "fastp" in " ".join(command)
        return subprocess.CompletedProcess(command, 1 if missing else 0, "", "missing")

    config = _config()
    with pytest.raises(RunReadinessError) as rejected:
        preflight_run_readiness(
            config,
            effective_methods=effective_method_closure(config),
            config_path=_envs(tmp_path),
            runner=runner,
        )
    assert "read_qc.short_reads.fastp" in str(rejected.value)
    assert "read_qc.hic_reads.fastp" in str(rejected.value)
    assert not (tmp_path / "data").exists()
