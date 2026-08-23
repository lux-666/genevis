"""Canonical genome-size resolution contract tests."""

import tempfile
from pathlib import Path

import pytest

from app.domains.assembly.constants import FLYE_METHOD, NEXTDENOVO_METHOD
from app.domains.genome_size import resolve_explicit_genome_size, resolve_genome_size
from app.domains.parameter_recommendation.facts import build_canonical_facts
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    RecommendationInputSnapshotV3,
)
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state
from app.stages.assembly.command_builder import resolve_assembly_parameters
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.stages.inventory.nodes import validate_inputs
from app.storage.frozen_inputs import freeze_session_inputs, validate_frozen_inputs
from app.storage.paths import SessionPaths


def _state():
    return initial_state(session_id="s1", input_manifest_ref="manifest.yaml")


def test_explicit_shared_value_wins_over_survey_measurement():
    state = _state()
    state["shared_parameters"] = {"estimated_genome_size": 2_000_000}
    state["stage_outputs"] = {
        "genome_survey": {
            "genome_size_prior": 1_500_000,
            "genome_size_source": "survey",
        }
    }

    assert resolve_genome_size(state) == {
        "value_bp": 2_000_000,
        "source": "provided",
        "source_path": "shared_parameters.estimated_genome_size",
    }


def test_manifest_inventory_value_is_an_explicit_prior():
    state = _state()
    state["stage_outputs"] = {"inventory": {"genome_size_estimate": 3_000_000}}

    assert resolve_explicit_genome_size(state) == {
        "value_bp": 3_000_000,
        "source": "provided",
        "source_path": "inventory.genome_size_estimate",
    }


def test_legacy_method_specific_genome_size_paths_are_not_accepted():
    state = _state()
    state["stage_outputs"] = {
        "assembly": {
            "flye_parameters": {"estimated_genome_size": 2_000_000},
            "nextdenovo_parameters": {"genome_size": 3_000_000},
        }
    }

    assert resolve_explicit_genome_size(state) is None


def test_stale_derived_value_does_not_mask_a_new_explicit_value():
    state = _state()
    state["shared_parameters"] = {"estimated_genome_size": 5_000_000}
    state["stage_outputs"] = {
        "assembly": {
            "resolved_genome_size": {
                "value_bp": 2_000_000,
                "source": "provided",
                "source_path": "shared_parameters.estimated_genome_size",
            }
        }
    }

    assert resolve_explicit_genome_size(state) == {
        "value_bp": 5_000_000,
        "source": "provided",
        "source_path": "shared_parameters.estimated_genome_size",
    }


def test_survey_handoff_requires_canonical_top_level_source():
    state = _state()
    state["stage_outputs"] = {
        "genome_survey": {
            "genome_size_prior": 4_000_000,
            "genome_size_source": "survey",
            "estimates": {"genome_size": 4_000_000},
        }
    }

    assert resolve_genome_size(state) == {
        "value_bp": 4_000_000,
        "source": "survey",
        "source_path": "genome_survey.genome_size_prior",
    }


def test_dead_nested_survey_path_is_not_accepted():
    state = _state()
    state["stage_outputs"] = {
        "genome_survey": {"estimates": {"genome_size_prior": 4_000_000}}
    }

    assert resolve_genome_size(state) is None


def test_deferred_assembly_accepts_missing_genome_size_at_freeze_boundary():
    """The eventual method owns genome-size requirements after survey evidence."""
    config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "assembly", "allow_local_execution": True},
            ],
            "assembly": {"assembly_route": "auto"},
        }
    )

    assert config.assembly is not None
    assert config.assembly.assembly_route == "auto"


@pytest.mark.parametrize(
    ("method_id", "l0_executor_values"),
    [
        (
            FLYE_METHOD,
            {
                "assembly_method_read_type": "nano-raw",
                "assembly_input_reads": ["/reads/long.fastq"],
                "assembly_output_dir": "/work/flye",
                "assembly_threads": 4,
            },
        ),
        (
            NEXTDENOVO_METHOD,
            {
                "assembly_method_read_type": "ont",
                "assembly_input_reads": "/work/input.fofn",
                "assembly_output_dir": "/work/nextdenovo",
                "assembly_threads": 4,
            },
        ),
    ],
)
def test_manifest_only_genome_size_reaches_assembly_l1_freeze(
    method_id,
    l0_executor_values,
):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reads = root / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        source_manifest = root / "manifest.yaml"
        source_manifest.write_text(
            "\n".join(
                [
                    "session_id: s1",
                    "genome_size_estimate: 2000000",
                    "assets:",
                    "  - asset_type: raw_long_reads",
                    f"    paths: [{reads}]",
                    "    platform: ont",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        source_config = root / "run_config.json"
        source_config.write_text(
            '{"requested_stages": []}\n',
            encoding="utf-8",
        )
        paths = SessionPaths(root / "data", "s1")
        freeze_session_inputs(
            paths,
            manifest_draft=source_manifest,
            run_config_draft=source_config,
            selected_methods={},
        )
        state = initial_state(
            session_id="s1",
            input_manifest_ref=paths.to_ref(paths.input_manifest_path),
            data_dir=str(paths.run_dir),
        )

        state = {
            **state,
            **validate_inputs(state, verified_inputs=validate_frozen_inputs(paths)),
        }
        assert state["shared_parameters"] == {
            "estimated_genome_size": 2_000_000,
        }
        facts = build_canonical_facts(
            shared_parameters=state["shared_parameters"],
            read_qc_output=None,
            genome_survey_result=None,
        )
        snapshot = RecommendationInputSnapshotV3(
            schema_version=3,
            method_id=method_id,
            task="recommend_initial",
            facts=facts,
            stage_context=AssemblyRecommendationContext(
                stage="assembly",
                assembly_route="ont",
                read_type="ont_raw",
                target_level="draft",
            ),
        )
        resolved = resolve_assembly_parameters(
            catalog=load_parameter_catalog(method_id),
            l0_executor_values=l0_executor_values,
            snapshot=snapshot,
            approved_l2={},
        )

        assert resolved.l1_facts["genome_size"] == 2_000_000
        assert resolved.effective_parameters["genome_size"] == 2_000_000
