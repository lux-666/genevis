"""Regression tests for detached inspect execution/readiness consistency."""

from __future__ import annotations

import json

from app.control.lifecycle import inspect_session
from app.storage.frozen_inputs import freeze_session_inputs
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.busco_fixture import ensure_test_host_envs
from tests.data_v2 import v2_run_dir


def test_corrupt_checkpoint_file_is_not_reported_as_durable_resume(tmp_path) -> None:
    session_id = "corrupt-checkpoint"
    run_dir = v2_run_dir(tmp_path / "data", session_id)
    paths = SessionPaths.from_run_dir(run_dir)
    reads = tmp_path / "reads.fastq"
    reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"session_id: {session_id}\n"
        "assets:\n"
        "  - asset_type: raw_long_reads\n"
        f"    paths:\n      - {reads}\n"
        "    platform: ont\n"
        "    format: fastq\n",
        encoding="utf-8",
    )
    run_config = tmp_path / "run_config.json"
    run_config.write_text(
        json.dumps(
            {"requested_stages": [{"stage": "read_qc", "allow_local_execution": False}]}
        ),
        encoding="utf-8",
    )
    freeze_session_inputs(
        paths,
        manifest_draft=manifest,
        run_config_draft=run_config,
        config_path=ensure_test_host_envs(tmp_path),
        selected_methods={},
    )
    Ledger(run_dir).create_session(session_id, "control/input_manifest.yaml")
    paths.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
    paths.checkpoint_db_path.write_bytes(b"not a sqlite database")

    summary = inspect_session(session_id, run_dir)

    assert summary["checkpoint_persistence"] == "sqlite"
    assert summary["execution"]["durable_progress"] == "unavailable"
    assert summary["durable_resume_available"] is False
