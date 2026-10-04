"""Recover source measurements without inventing scalar values or input scope."""

import json
import sys
from copy import deepcopy

import pytest

from app.knowledge.assembly_cases import AssemblyKnowledge
from app.knowledge.assembly_retrieval import retrieve_assembly_cases
from app.knowledge.v5_projection import _number, project_paper
from tests.test_assembly_case_consumption import projection_fixture


@pytest.mark.parametrize(
    "raw, unit, expected",
    [
        ("~7 gigabases", "bp", 7e9),
        ("8.2 kilobases", "bp", 8200),
        ("42.98 Gbp", "bp", 42.98e9),
        ("17.59 Kb", "bp", 17590),
        ("roughly 170x", "x", 170),
        ("∼122×", "x", 122),
        ("61-fold", "x", 61),
        ("0.358 %", "fraction", 0.00358),
        ("13.88 kb (13,877 bp)", "bp", 13877),
        ("21.44 Gb (21,439,216,780 bp)", "bp", 21439216780),
    ],
)
def test_explicit_scalar_units_and_repeated_rounding(raw, unit, expected):
    assert _number(raw, unit) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw, unit",
    [
        (">16 kb", "bp"),
        ("460.24-480.15 Mbp", "bp"),
        ("just under 0.6%", "fraction"),
        ("nearly 636 Mbp", "bp"),
        ("2.2 Tb (~150x coverage, ~18 kbp mean read length)", "bp"),
        ("21 Gb (30 Gb)", "bp"),
        ("506.65", "bp"),
        ("12,34 bp", "bp"),
        ("101%", "fraction"),
        ("diploid", "count"),
        ("2x", "count"),
    ],
)
def test_bounds_compound_values_missing_units_and_wrong_dimensions_stay_text(raw, unit):
    assert _number(raw, unit) is None


def scalar_fixture():
    graph, selections = projection_fixture()
    graph["measurements"][0].update(state_ids=["r"], basis="each")
    selections[0]["input_states"] = {"r": "filtered"}
    return graph, selections


def test_each_new_case_requires_parameter_eligibility():
    graph, selections = projection_fixture()
    del selections[0]["parameter_eligible"]
    with pytest.raises(ValueError, match="parameter_eligible"):
        project_paper("p", graph, selections)


def test_explicit_header_unit_and_input_state_enable_numeric_retrieval():
    graph, selections = scalar_fixture()
    graph["measurements"][0].update(metric="Coverage depth (×)", value="30")
    spec = selections[0]["features"]["ont_coverage"]
    spec.update(source_unit="x", unit_basis="measurement.metric: Coverage depth (×)")
    paper = project_paper("p", graph, selections)
    observed = paper.cases[0].observations[0]
    assert (observed.value, observed.unit, observed.state) == (30, "x", "filtered")
    assert "Coverage depth (×)" in observed.basis
    query = {"text": "", "observations": [{**observed.model_dump(), "value": 27}]}
    result = retrieve_assembly_cases(
        AssemblyKnowledge(papers=(paper,), software={}), query, purpose="method_choice"
    )
    assert result["trace"][0]["material"] == pytest.approx(0.9)
    assert result["trace"][0]["lexical"] == 0
    query["observations"][0]["state"] = "raw"
    assert not retrieve_assembly_cases(
        AssemblyKnowledge(papers=(paper,), software={}), query, purpose="method_choice"
    )["evidence"]["cases"]
    del spec["unit_basis"]
    with pytest.raises(ValueError, match="unit basis"):
        project_paper("p", graph, selections)


def test_repeated_measurements_do_not_choose_away_conflicts():
    graph, selections = scalar_fixture()
    graph["measurements"].append(
        {**graph["measurements"][0], "id": "q2", "value": "50x"}
    )
    selections[0]["features"]["ont_coverage"] = [
        {"measurement": "q", "unit": "x"},
        {"measurement": "q2", "unit": "x"},
    ]
    paper = project_paper("p", graph, selections)
    assert [o.value for o in paper.cases[0].observations] == [30, 50]
    result = retrieve_assembly_cases(
        AssemblyKnowledge(papers=(paper,), software={}),
        {"text": "", "observations": [paper.cases[0].observations[0].model_dump()]},
        purpose="method_choice",
    )
    assert not result["evidence"]["cases"]


def test_source_only_and_unnormalizable_measurements_are_retained_once():
    graph, selections = scalar_fixture()
    graph["measurements"][0]["value"] = "30x to 50x"
    graph["measurements"].append({**graph["measurements"][0], "id": "q2"})
    selections[0]["reported_measurements"] = ["q", "q2"]
    paper = project_paper("p", graph, selections)
    case = paper.cases[0]
    assert not case.observations
    assert len(case.content["reported_measurements"]) == 2
    assert "ont_coverage:q" in case.content["normalization_notes"]
    graph["measurements"][1]["state_ids"] = ["a"]
    with pytest.raises(ValueError, match="case materials"):
        project_paper("p", graph, selections)


def test_excluded_scope_never_produces_an_observation():
    graph, selections = scalar_fixture()
    selections[0]["features"]["ont_coverage"]["exclude_reason"] = (
        "Input selects a subset; measurement describes parent reads."
    )
    case = project_paper("p", graph, selections).cases[0]
    assert not case.observations
    assert case.content["reported_measurements"][0]["value"] == "30x"
    assert "subset" in case.content["normalization_notes"]["ont_coverage:q"]
    bad = deepcopy(selections)
    bad[0]["input_states"]["a"] = "raw"
    with pytest.raises(ValueError, match="selected read input"):
        project_paper("p", graph, bad)


def test_hic_measurement_keeps_its_platform():
    graph, selections = scalar_fixture()
    selections[0]["inputs"]["r"] = "hic"
    selections[0]["features"] = {"hic_coverage": {"measurement": "q", "unit": "x"}}
    observed = project_paper("p", graph, selections).cases[0].observations[0]
    assert observed.platform == "hic"
    assert observed.name == "coverage"


def test_downstream_outcome_keeps_gaps_on_its_evaluated_state():
    graph, selections = scalar_fixture()
    graph["states"].append({"id": "final", "kind": "assembly", "sample_ids": ["s"]})
    graph["steps"].append(
        {
            "id": "polish",
            "name": "polish",
            "sample_ids": ["s"],
            "inputs": [{"state_id": "a", "role": "target"}],
            "output_state_ids": ["final"],
        }
    )
    graph["measurements"].append(
        {
            "id": "qv",
            "state_ids": ["final"],
            "metric": "QV",
            "value": "40",
            "basis": "each",
        }
    )
    graph["gaps"].append(
        {
            "id": "gap-final",
            "kind": "conflict",
            "affected_ids": ["final"],
            "issue": "Final assembly quality differs between sources.",
        }
    )
    selections[0]["outcomes"] = ["qv"]
    content = project_paper("p", graph, selections).cases[0].content
    assert content["outcomes"][0]["attribution"] == "downstream_context"
    assert content["gaps"][0]["issue"] == graph["gaps"][0]["issue"]


def test_derived_coverage_requires_unique_same_platform_operands():
    graph, selections = scalar_fixture()
    graph["states"].append(
        {
            "id": "g",
            "kind": "biological_genome",
            "sample_ids": ["s"],
            "sample_scope": "individual",
            "origin": "study",
        }
    )
    graph["measurements"][0].update(metric="total bases", value="3 Gb")
    graph["measurements"].append(
        {
            "id": "size",
            "state_ids": ["g"],
            "metric": "genome size",
            "value": "100 Mb",
            "basis": "each",
        }
    )
    binding = selections[0]
    binding["features"] = {
        "ont_total_bases": {"measurement": "q", "unit": "bp"},
        "genome_size": {"measurement": "size", "unit": "bp"},
    }
    binding["derived"] = {"ont_coverage": ["ont_total_bases", "genome_size"]}
    case = project_paper("p", graph, selections).cases[0]
    assert case.observations[-1].value == 30
    assert case.observations[-1].origin == "derived"
    binding["derived"] = {"hifi_coverage": ["ont_total_bases", "genome_size"]}
    with pytest.raises(ValueError, match="unsupported derived"):
        project_paper("p", graph, selections)
    binding["derived"] = {"ont_coverage": ["ont_total_bases", "genome_size"]}
    binding["features"]["genome_size"] = [binding["features"]["genome_size"]] * 2
    case = project_paper("p", graph, selections).cases[0]
    assert all(o.name != "coverage" for o in case.observations)
    assert "unique operands" in case.content["normalization_notes"]["ont_coverage"]


def test_export_response_files_reports_empty_material_index(
    tmp_path, monkeypatch, capsys
):
    from scripts.build_assembly_cases import main

    graph, selections = projection_fixture()
    (tmp_path / "p-response.json").write_text(json.dumps(graph))
    (tmp_path / "selection.json").write_text(json.dumps(selections))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_assembly_cases",
            "--extractions",
            str(tmp_path),
            "--selection",
            str(tmp_path / "selection.json"),
            "--output",
            str(tmp_path / "cases"),
            "--filename-template",
            "{paper}-response.json",
        ],
    )
    main()
    output = capsys.readouterr()
    assert '"with_observations": 0' in output.out
    assert "no numeric material index" in output.err
    assert (tmp_path / "cases/p.json").exists()


@pytest.mark.parametrize("eligible", [True, False, None, "true", 1])
def test_import_requires_explicit_eligibility_and_preserves_reviewed_cases(
    tmp_path, monkeypatch, eligible
):
    from scripts.build_assembly_cases import main

    graph, selections = scalar_fixture()
    if eligible is None:
        del selections[0]["parameter_eligible"]
    else:
        selections[0]["parameter_eligible"] = eligible
    source = tmp_path / "graphs"
    source.mkdir()
    (source / "p.json").write_text(json.dumps(graph))
    selection = tmp_path / "bindings.json"
    selection.write_text(json.dumps(selections))
    output = tmp_path / "cases"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_assembly_cases",
            "--extractions",
            str(source),
            "--selection",
            str(selection),
            "--output",
            str(output),
        ],
    )
    if type(eligible) is not bool:
        with pytest.raises(ValueError, match="missing parameter eligibility review"):
            main()
        assert not output.exists()
        return
    main()
    destination = output / "p.json"
    imported = destination.read_bytes()
    target = tmp_path / "imported.json"
    destination.rename(target)
    destination.symlink_to(target)
    with pytest.raises(ValueError, match="refusing symlink case destination"):
        main()
    assert target.read_bytes() == imported
    destination.unlink()
    target.rename(destination)
    payload = json.loads(destination.read_text())
    assert payload["cases"][0]["parameter_eligible"] is eligible
    payload["cases"][0]["observations"][0]["basis"] += "; reviewed source locator"
    destination.write_text(json.dumps(payload))
    reviewed = destination.read_bytes()
    with pytest.raises(ValueError, match="refusing to overwrite different case"):
        main()
    assert destination.read_bytes() == reviewed


def test_explicit_components_recover_metrics_without_confusing_library_size():
    graph, selections = scalar_fixture()
    source = (
        "4.1 Gb (~96.7x; N50 10,027 bp; mean read length 9,196.9 bp; "
        "longest read 32,687 bp; library size 15 kb)"
    )
    graph["measurements"][0]["value"] = source
    selections[0]["features"] = {
        "ont_" + name: {
            "measurement": "q",
            "unit": unit,
            "component": {"text": text, "label": label},
        }
        for name, unit, text, label in (
            ("total_bases", "bp", "4.1 Gb", ""),
            ("coverage", "x", "~96.7x", ""),
            ("read_n50", "bp", "N50 10,027 bp", "N50"),
            (
                "mean_read_length",
                "bp",
                "mean read length 9,196.9 bp",
                "mean read length",
            ),
        )
    }
    case = project_paper("p", graph, selections).cases[0]
    assert [o.value for o in case.observations] == pytest.approx(
        [4.1e9, 96.7, 10027, 9196.9]
    )
    assert all(source in o.basis for o in case.observations)
    assert len(case.content["reported_measurements"]) == 1
    selections[0]["features"]["ont_mean_read_length"]["component"] = {
        "text": "library size 15 kb",
        "label": "library size",
    }
    with pytest.raises(ValueError, match="label does not match"):
        project_paper("p", graph, selections)


@pytest.mark.parametrize("source", [">30x", "20-30x", "30x subset", "30x; 30x"])
def test_component_cannot_pick_scalar_from_qualified_or_repeated_clause(source):
    graph, selections = scalar_fixture()
    graph["measurements"][0]["value"] = source
    selections[0]["features"]["ont_coverage"]["component"] = {"text": "30x"}
    with pytest.raises(ValueError, match="complete source clause"):
        project_paper("p", graph, selections)


def test_component_does_not_bypass_scope_or_exclusion():
    graph, selections = scalar_fixture()
    graph["measurements"][0].update(value="3 Gb; coverage >30x")
    spec = selections[0]["features"]["ont_coverage"]
    spec["component"] = {"text": "coverage >30x", "label": "coverage"}
    assert not project_paper("p", graph, selections).cases[0].observations
    graph["measurements"][0].update(value="3 Gb; coverage 30x", basis="combined")
    spec["component"]["text"] = "coverage 30x"
    assert not project_paper("p", graph, selections).cases[0].observations
    graph["measurements"][0]["basis"] = "each"
    spec["exclude_reason"] = "Measurement describes parent reads, not input subset."
    assert not project_paper("p", graph, selections).cases[0].observations


def test_component_does_not_choose_one_of_conflicting_repeated_quantities():
    graph, selections = scalar_fixture()
    graph["measurements"][0]["value"] = "30x (50x)"
    selections[0]["features"]["ont_coverage"]["component"] = {"text": "30x"}
    assert not project_paper("p", graph, selections).cases[0].observations
