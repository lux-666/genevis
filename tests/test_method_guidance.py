"""Tests for the structured assembly method-guidance registry."""

from __future__ import annotations

from pathlib import Path

import pytest

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
        "hifiasm.connectivity.low_coverage.v1",
        "hifiasm.purging.heterozygosity.v1",
        "hifiasm.repeats.graph_handling.v1",
        "hifiasm.retry.over_purging.v1",
        "hifiasm.retry.under_purging.v1",
    }


def test_model_cards_are_method_filtered_and_deterministic():
    registry = load_method_guidance()

    hifi_cards = registry.model_cards_for_method("hifiasm")
    ont_cards = registry.model_cards_for_method("hifiasm_ont")

    assert [card["guidance_id"] for card in hifi_cards] == sorted(registry.by_id)
    assert hifi_cards == ont_cards
    assert hifi_cards == registry.model_cards_for_method("hifiasm")
    assert all(set(card) == {"guidance_id", "methods", "parameters", "relevant_metrics", "guidance", "caveats"} for card in hifi_cards)


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
    methods: [hifiasm]
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
        ("methods: [flye]", "unknown method id"),
        ("parameters: []", "at least 1 item"),
        ("guidance: '   '", "must not be blank"),
    ],
)
def test_invalid_guidance_contract_is_rejected(tmp_path: Path, replacement: str, message: str):
    base = """
guidance:
  - guidance_id: test.guidance.v1
    methods: [hifiasm]
    parameters: [purge_level]
    relevant_metrics: [genome_survey.heterozygosity]
    guidance: Mechanism text.
"""
    old = {
        "methods: [flye]": "methods: [hifiasm]",
        "parameters: []": "parameters: [purge_level]",
        "guidance: '   '": "guidance: Mechanism text.",
    }[replacement]
    path = _write_guidance(tmp_path, base.replace(old, replacement))

    with pytest.raises(ValueError, match=message):
        load_method_guidance(path)


def test_unknown_method_projection_is_rejected():
    registry = load_method_guidance()

    with pytest.raises(ValueError, match="Unknown method id"):
        registry.model_cards_for_method("flye")
