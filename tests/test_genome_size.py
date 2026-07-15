"""Canonical genome-size resolution contract tests."""

import pytest

from app.domains.genome_size import resolve_explicit_genome_size, resolve_genome_size
from app.schemas.manifest import InputAssetSpec, InputManifest
from app.schemas.run_config import RunConfig, validate_genome_size_execution_plan
from app.schemas.state import initial_state


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


def _manifest(*, genome_size_estimate=None):
    return InputManifest(
        session_id="s1",
        genome_size_estimate=genome_size_estimate,
        assets=[InputAssetSpec(asset_type="raw_long_reads", paths=["reads.fastq"], platform="ont")],
    )


def test_preflight_rejects_flye_when_no_stage_can_produce_genome_size():
    config = RunConfig.model_validate(
        {
            "local_exec": ["assembly"],
            "requested_stages": ["assembly"],
            "assembly": {"assembly_method": "flye", "flye_parameters": {"read_type": "nano-raw", "threads": 4}},
        }
    )

    with pytest.raises(ValueError, match="schedule executable genome_survey"):
        validate_genome_size_execution_plan(config, _manifest())


def test_preflight_allows_flye_to_wait_for_an_earlier_executable_survey():
    config = RunConfig.model_validate(
        {
            "local_exec": ["genome_survey", "assembly"],
            "requested_stages": ["genome_survey", "assembly"],
            "genome_survey": {"survey_parameters": {"kmer_size": 21, "hash_size": "1G", "threads": 4}},
            "assembly": {"assembly_method": "flye", "flye_parameters": {"read_type": "nano-raw", "threads": 4}},
        }
    )

    validate_genome_size_execution_plan(config, _manifest())


def test_preflight_allows_manifest_genome_size_without_survey():
    config = RunConfig.model_validate(
        {
            "local_exec": ["assembly"],
            "requested_stages": ["assembly"],
            "assembly": {"assembly_method": "flye", "flye_parameters": {"read_type": "nano-raw", "threads": 4}},
        }
    )

    validate_genome_size_execution_plan(config, _manifest(genome_size_estimate=2_000_000))
