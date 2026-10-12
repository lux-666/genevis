"""Reject impossible sequential routes before freezing or entering a consumer."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.control.workflow_plan import (
    seed_current_workflow_step,
    workflow_steps_from_run_config,
)
from app.graph.builder import build_graph
from app.schemas.resource_ref import ResourceRef
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state
from app.storage.frozen_inputs import (
    FrozenInputValidationError,
    freeze_session_inputs,
    prepare_session_inputs,
)
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


def _manifest(root: Path, *, assembly: bool = False, parents: bool = False) -> dict:
    reads = root / "reads.fq"
    reads.write_text("@r\nACGT\n+\n!!!!\n")
    assets = [
        {
            "asset_type": "raw_long_reads",
            "paths": [str(reads)],
            "platform": "pacbio_hifi",
            "format": "fastq",
            "metadata": {"intake_role": "trio_maternal_reads"} if parents else {},
        }
    ]
    if assembly:
        fasta = root / "assembly.fa"
        fasta.write_text(">ctg\nACGT\n")
        assets.append(
            {
                "asset_type": "assembly_fasta_registered",
                "paths": [str(fasta)],
                "format": "fasta",
            }
        )
    return {
        "session_id": "s1",
        "assets": assets,
        "user_metadata": {"target_species": "Fixture species"},
    }


def _config(*stages: str, local: bool = True) -> dict:
    return {
        "requested_stages": [
            {"stage": stage, "allow_local_execution": local} for stage in stages
        ],
        "genome_survey": {
            "survey_parameters": {"kmer_size": 21, "hash_size": "1M", "threads": 1}
        },
    }


@pytest.mark.parametrize(
    "stages,parents,assembly,error",
    [
        (("t2t",), False, False, "step-001-t2t.*input assembly"),
        (("t2t", "assembly"), False, False, "step-001-t2t.*input assembly"),
        (("assembly_dedup",), False, False, "input assembly"),
        (("assembly_review",), False, False, "input assembly"),
        (("gapcloser",), False, False, "input assembly"),
        (("base_polish",), False, False, "input assembly"),
        (("read_qc",), True, False, "offspring"),
        (("genome_survey", "assembly"), True, False, "offspring"),
        (("assembly",), True, False, "offspring long reads"),
        (("assembly", "genome_survey"), False, False, "genome_size_estimate"),
        (("hic_scaffolding",), False, True, "paired Hi-C reads"),
        (("base_polish",), True, True, "offspring long reads"),
    ],
)
def test_impossible_inputs_fail_before_run_layout(
    tmp_path, stages, parents, assembly, error
):
    paths = SessionPaths(tmp_path / "data", "s1")
    with pytest.raises(FrozenInputValidationError, match=error):
        freeze_session_inputs(
            paths,
            manifest_draft=_manifest(tmp_path, assembly=assembly, parents=parents),
            run_config_draft=_config(*stages, local="hic_scaffolding" not in stages),
        )
    assert not paths.data_root.exists()


@pytest.mark.parametrize(
    "stages,assembly",
    [
        (("read_qc",), False),
        (("genome_survey",), False),
        (("genome_survey", "assembly", "t2t", "t2t"), False),
        (("t2t", "assembly_review", "t2t"), True),
        (("gapcloser",), True),
    ],
)
def test_standalone_and_repeated_routes_preserve_literal_order(
    tmp_path, stages, assembly
):
    prepared = prepare_session_inputs(
        manifest_draft=_manifest(tmp_path, assembly=assembly),
        run_config_draft=_config(*stages),
    )
    assert prepared.run_config.requested_stage_names() == list(stages)


def test_nonexecuted_assembly_cannot_supply_a_later_consumer(tmp_path):
    with pytest.raises(
        FrozenInputValidationError, match="step-002-t2t.*input assembly"
    ):
        prepare_session_inputs(
            manifest_draft=_manifest(tmp_path, assembly=True),
            run_config_draft=_config("assembly", "t2t", local=False),
        )


@pytest.mark.parametrize("stage", ["assembly", "genome_survey"])
def test_explicit_genome_size_is_sufficient(tmp_path, stage):
    config = _config(stage)
    manifest = _manifest(tmp_path)
    manifest["genome_size_estimate"] = 1000
    if stage == "genome_survey":
        manifest = _manifest(tmp_path, assembly=True, parents=True)
        manifest["genome_size_estimate"] = 1000
    prepare_session_inputs(manifest_draft=manifest, run_config_draft=config)


def test_selected_fastp_requires_its_own_read_family(tmp_path):
    config = _config("read_qc")
    config["read_qc"] = {"short_reads": {}}
    with pytest.raises(FrozenInputValidationError, match="paired short_reads"):
        prepare_session_inputs(
            manifest_draft=_manifest(tmp_path), run_config_draft=config
        )


def test_qc_preflight_uses_canonical_clean_short_read_priority(tmp_path):
    paths = [tmp_path / name for name in ("raw.fq", "clean_r1.fq", "clean_r2.fq")]
    for path in paths:
        path.write_text("@r\nACGT\n+\n!!!!\n")
    config = _config("read_qc")
    config["read_qc"] = {"short_reads": {}}
    prepare_session_inputs(
        manifest_draft={
            "session_id": "s1",
            "assets": [
                {
                    "asset_type": "raw_short_dna_reads",
                    "paths": [str(paths[0])],
                    "platform": "illumina",
                    "format": "fastq",
                },
                {
                    "asset_type": "clean_short_dna_reads",
                    "paired_reads": [{"r1": str(paths[1]), "r2": str(paths[2])}],
                    "platform": "illumina",
                    "format": "fastq",
                },
            ],
        },
        run_config_draft=config,
    )


@pytest.mark.parametrize(
    "producer,stage",
    [
        (None, None),
        ("step-000-inventory", "inventory"),
        ("step-002-t2t", "t2t"),
        ("step-999-t2t", "t2t"),
        ("step-001-t2t", "assembly"),
    ],
)
def test_consumer_rejects_wrong_occurrence_even_with_same_path(producer, stage):
    state = _repeated_consumer_state()
    if producer is not None:
        state["resource_refs"]["assembly"] = ResourceRef(
            "assembly", ["/data/assembly.fa"], stage, producer
        ).to_dict()
    before = deepcopy(state)
    update = seed_current_workflow_step(state)
    assert update["supervision_state"] == "failed_terminal"
    assert "requires assembly from step-001-t2t" in update["last_error"]
    assert update["workflow_step_outcome"]["outcome"] == "failed"
    assert state == before


def _repeated_consumer_state():
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/input_manifest.yaml",
        workflow_steps=workflow_steps_from_run_config(
            RunConfig.model_validate(_config("t2t", "t2t"))
        ),
    )
    state.update(
        current_stage="t2t", workflow_step_index=2, entered_workflow_step_index=1
    )
    return state


def test_consumer_accepts_exact_handoff_including_reused_fasta():
    state = _repeated_consumer_state()
    state["resource_refs"]["assembly"] = ResourceRef(
        "assembly", ["/data/assembly.fa"], "t2t", "step-001-t2t"
    ).to_dict()
    update = seed_current_workflow_step(state)
    assert update["entered_workflow_step_index"] == 2


def test_entry_rejection_records_failure_without_dispatching_stage(tmp_path):
    run_dir = v2_run_dir(tmp_path, "s1")
    state = _repeated_consumer_state()
    state["data_dir"] = str(run_dir)
    state["workflow_step_outcome"] = {
        "workflow_step_id": "step-001-t2t",
        "stage": "t2t",
        "outcome": "skipped",
        "reason": "fixture producer did not deliver",
    }
    state["resource_refs"]["assembly"] = ResourceRef(
        "assembly", ["/data/assembly.fa"], "inventory", "step-000-inventory"
    ).to_dict()
    ledger = Ledger(run_dir)
    ledger.create_session("s1", state["input_manifest_ref"])
    for name in state["workflow_steps"][1]["selected_method_names"]:
        ledger.record_method_disposition(
            "s1", "t2t", "step-001-t2t", name, "skipped", reason="fixture skip"
        )
    stage = Mock(side_effect=AssertionError("consumer must not run"))
    with patch("app.graph.builder.build_t2t_stage", return_value=stage):
        graph = build_graph(backend="memory", data_dir=str(run_dir))
        result = graph.invoke(state, {"configurable": {"thread_id": "s1"}})
    assert result["supervision_state"] == "failed_terminal"
    stage.assert_not_called()
    rows = ledger.read_event_rows("s1")
    terminals = [
        (r["workflow_step_id"], r["event_type"])
        for r in rows
        if r["event_type"] in {"stage_completed", "stage_skipped", "stage_failed"}
    ]
    assert terminals == [
        ("step-001-t2t", "stage_skipped"),
        ("step-002-t2t", "stage_failed"),
    ]
    dispositions = [
        r["summary"]
        for r in rows
        if r["event_type"] == "method_disposition_recorded"
        and r["workflow_step_id"] == "step-002-t2t"
    ]
    assert {r["method_name"] for r in dispositions} == set(
        state["workflow_steps"][2]["selected_method_names"]
    )
    assert all(r["disposition"] == "not_started" for r in dispositions)
    assert all(r["reason"] == result["last_error"] for r in dispositions)

    # Replay both sides of the controller checkpoint without duplicate facts.
    for checkpoint in (state, result):
        replay = graph.invoke(checkpoint, {"configurable": {"thread_id": "s1"}})
        assert replay["supervision_state"] == "failed_terminal"
        assert replay["last_error"] == result["last_error"]
        assert ledger.read_event_rows("s1") == rows
    stage.assert_not_called()
