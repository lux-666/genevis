from __future__ import annotations

import re

import pytest

from app.domains.parameter_recommendation.facts import build_canonical_facts


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        (
            {"shared_parameters": {"species_facts": []}},
            "shared_parameters.species_facts",
        ),
        (
            {"read_qc_output": []},
            "read_qc",
        ),
        (
            {"genome_survey_result": []},
            "genome_survey",
        ),
    ],
)
def test_canonical_facts_reject_non_mapping_present_source_contracts(
    overrides: dict,
    field: str,
) -> None:
    inputs = {
        "shared_parameters": {},
        "read_qc_output": None,
        "genome_survey_result": None,
    }
    inputs.update(overrides)

    with pytest.raises(ValueError, match=re.escape(f"{field} must be a mapping")):
        build_canonical_facts(**inputs)


def test_canonical_facts_project_shared_read_type() -> None:
    facts = build_canonical_facts(
        shared_parameters={"read_type": "ont_raw"},
        read_qc_output=None,
        genome_survey_result=None,
    )

    assert facts.reads.read_type == "ont_raw"
