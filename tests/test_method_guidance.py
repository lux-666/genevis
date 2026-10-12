"""Tests for the structured assembly method-guidance registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domains.assembly.constants import (
    ASSEMBLY_CANDIDATES,
    HIFIASM_METHOD,
    HIFIASM_ONT_METHOD,
)
from app.domains.assembly.method_guidance import (
    DEFAULT_GUIDANCE_PATH,
    load_method_guidance,
)


def _write_guidance(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "guidance.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_checked_in_guidance_loads_and_covers_mvp_areas():
    registry = load_method_guidance()

    assert DEFAULT_GUIDANCE_PATH.exists()
    assert set(registry.by_id) == {
        "flye.disjointigs.coverage.v1",
        "flye.overlaps.connectivity.v1",
        "nextdenovo.reads.cutoff.v1",
        "hifiasm.connectivity.low_coverage.v1",
        "hifiasm.purging.heterozygosity.v1",
        "hifiasm.repeats.graph_handling.v1",
        "hifiasm.retry.over_purging.v1",
        "hifiasm.retry.under_purging.v1",
    }


def test_model_cards_are_method_filtered_and_deterministic():
    registry = load_method_guidance()

    hifi_cards = registry.model_cards_for_method(HIFIASM_METHOD)
    ont_cards = registry.model_cards_for_method(HIFIASM_ONT_METHOD)

    assert [card["guidance_id"] for card in hifi_cards] == sorted(
        key for key in registry.by_id if key.startswith("hifiasm.")
    )
    assert hifi_cards == ont_cards
    assert hifi_cards == registry.model_cards_for_method(HIFIASM_METHOD)
    assert all(
        set(card)
        == {
            "guidance_id",
            "methods",
            "parameters",
            "relevant_metrics",
            "guidance",
            "caveats",
        }
        for card in hifi_cards
    )


def test_registry_exposes_fail_closed_id_resolution():
    registry = load_method_guidance()

    record = registry.require("hifiasm.purging.heterozygosity.v1")
    assert record.parameters == ("purge_level", "purge_similarity", "purge_max")
    with pytest.raises(ValueError, match="Unknown guidance_id"):
        registry.require("missing")


def test_duplicate_guidance_ids_are_rejected(tmp_path: Path):
    path = _write_guidance(
        tmp_path,
        """
guidance:
  - &item
    guidance_id: duplicate
    methods: [assembly.candidate.hifiasm]
    parameters: [purge_level]
    relevant_metrics: [genome_survey.heterozygosity]
    guidance: First card.
  - <<: *item
""",
    )

    with pytest.raises(ValueError, match="Duplicate guidance_id"):
        load_method_guidance(path)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("methods: [hifiasm]", "unknown method id"),
        ("parameters: []", "at least 1 item"),
        ("guidance: '   '", "must not be blank"),
    ],
)
def test_invalid_guidance_contract_is_rejected(
    tmp_path: Path, replacement: str, message: str
):
    base = """
guidance:
  - guidance_id: test.guidance.v1
    methods: [assembly.candidate.hifiasm]
    parameters: [purge_level]
    relevant_metrics: [genome_survey.heterozygosity]
    guidance: Mechanism text.
"""
    old = {
        "methods: [hifiasm]": "methods: [assembly.candidate.hifiasm]",
        "parameters: []": "parameters: [purge_level]",
        "guidance: '   '": "guidance: Mechanism text.",
    }[replacement]
    path = _write_guidance(tmp_path, base.replace(old, replacement))

    with pytest.raises(ValueError, match=message):
        load_method_guidance(path)


def test_unknown_method_projection_is_rejected():
    registry = load_method_guidance()

    with pytest.raises(ValueError, match="Unknown method id"):
        registry.model_cards_for_method("hifiasm")


@pytest.mark.parametrize("method_id", ASSEMBLY_CANDIDATES)
def test_guidance_parameters_belong_to_the_target_adjustable_catalog(method_id):
    from app.domains.parameter_recommendation.catalog import (
        load_method_parameter_catalog,
    )

    cards = load_method_guidance().model_cards_for_method(method_id)
    catalog = load_method_parameter_catalog(method_id)
    assert cards
    for card in cards:
        assert card["methods"] == [method_id] or method_id in card["methods"]
        for name in card["parameters"]:
            assert catalog.parameters[name].layers.l2 is not None
