"""v5-derived evidence through retrieval, real DTO/rendering and frozen runs."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.domains.assembly.material_query import build_material_query
from app.domains.assembly.software_knowledge import capture_software_knowledge
from app.domains.parameter_recommendation.resolution import resolve_parameters
from app.knowledge.assembly_cases import (
    AssemblyCase,
    AssemblyKnowledge,
    CasePaper,
    Observation,
    SourceMethod,
    json_size,
    load_case_papers,
)
from app.knowledge.assembly_retrieval import (
    project_assembly_evidence,
    project_method_choice_case,
    project_parameter_case,
    retrieve_assembly_cases,
)
from app.knowledge.assembly_runtime import current_knowledge, initial_material_query
from app.knowledge.v5_projection import project_paper
from app.llm.parameters.assembly.prompt import render_assembly_parameter_messages
from app.llm.parameters.assembly.request import (
    build_parameter_request_dto,
    validate_parameter_basis,
)
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalRecommendationFacts,
)
from app.schemas.run_config import RunConfig
from app.stages.assembly.nodes import infer_input_profile, resolve_extended_inputs
from app.stages.assembly.parameters.basis import build_parameter_basis_snapshot
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.storage.frozen_inputs import (
    freeze_session_inputs,
    prepare_session_inputs,
    validate_frozen_inputs,
)
from tests import test_frozen_inputs as frozen_fixtures
from tests import test_parameter_resolution_validation as resolver_fixtures
from tests import test_phase8_slice1 as route_fixtures
from tests import test_phase10_parameter_catalog as attempt_fixtures

TARGET = "assembly.candidate.hifiasm"


def observation(size: float) -> Observation:
    return Observation(
        name="genome_size",
        value=size,
        unit="bp",
        object_id="sample",
        basis="explicit genome estimate",
    )


def paper(
    pid: str, tool: str, size: float, *, settings: tuple[str, ...] = ()
) -> CasePaper:
    return CasePaper(
        schema_version="recommendation-cases.v1",
        paper_id=pid,
        paper={"title": "Synthetic repeat-rich diploid genome"},
        evidence_scope="synthetic fixture",
        cases=(
            AssemblyCase(
                case_id=pid + ":assemble",
                configuration_id=pid + ":config",
                method=SourceMethod(tool=tool, settings=settings),
                observations=(observation(size),),
                input_platforms=("ont",),
                execution_status="performed",
                route_roles=("alternative",),
                content={
                    "action": "repeat-rich diploid genome assembly",
                    "notes": ["assembly outcome, not parameter effect"],
                },
            ),
        ),
    )


def snapshot() -> AssemblyKnowledge:
    return AssemblyKnowledge(
        software=capture_software_knowledge(),
        papers=(
            paper("nd", "NextDenovo", 420e6, settings=("read_cutoff = 10000",)),
            paper("hf", "hifiasm", 3e9, settings=("-l 2",)),
        ),
    )


def query() -> dict:
    return {
        "observations": [observation(420e6).model_dump()],
        "text": "repeat-rich diploid",
        "platforms": ["hifi"],
        "read_binding": {"paths": ["/private/current/reads.fastq"]},
    }


def test_zero_similarity_is_not_admitted_or_labelled_as_a_match():
    ploidy = Observation(
        name="ploidy", value=4, unit="count", object_id="sample", basis="reported"
    )
    source = paper("poly", "hifiasm", 420e6)
    source = source.model_copy(
        update={
            "cases": (source.cases[0].model_copy(update={"observations": (ploidy,)}),)
        }
    )
    corpus = snapshot().model_copy(update={"papers": (source,)})
    current = {"observations": [{**ploidy.model_dump(), "value": 2}], "text": ""}
    assert not retrieve_assembly_cases(corpus, current, purpose="method_choice")[
        "evidence"
    ]["cases"]
    current["text"] = "repeat-rich"
    matched = retrieve_assembly_cases(corpus, current, purpose="method_choice")[
        "evidence"
    ]["cases"][0]["match"]
    assert matched["matched_features"] == []
    assert matched["differing_features"] == ["ploidy"]
    assert matched["missing"] == []
    assert matched["comparisons"][0]["query_value"] == 2
    assert matched["comparisons"][0]["case_value"] == 4
    assert matched["comparisons"][0]["similarity"] == 0


def test_comparisons_keep_each_query_observation_identity():
    reference = observation(420e6).model_dump()
    current = {
        "observations": [
            {**reference, "value": 420, "unit": "Mb"},
            reference,
            {**reference, "state": "raw"},
            {**reference, "platform": "hifi"},
        ],
        "text": "",
    }
    result = retrieve_assembly_cases(snapshot(), current, purpose="method_choice")
    match = result["evidence"]["cases"][0]["match"]
    assert match["matched_features"] == ["genome_size"]
    assert match["missing"] == ["genome_size"] * 3
    assert len(match["comparisons"]) == 1
    assert match["comparisons"][0]["unit"] == "bp"
    assert result["trace"][0]["material"] == 0.25


def test_partial_numeric_similarity_is_kept_without_unknown_read_wildcards():
    current = {"observations": [observation(400e6).model_dump()], "text": ""}
    case = retrieve_assembly_cases(snapshot(), current, purpose="method_choice")[
        "evidence"
    ]["cases"][0]
    assert case["match"]["matched_features"] == ["genome_size"]
    assert case["match"]["differing_features"] == ["genome_size"]
    assert 0 < case["match"]["comparisons"][0]["similarity"] < 1
    current["observations"][0]["state"] = "raw"
    assert not retrieve_assembly_cases(snapshot(), current, purpose="method_choice")[
        "evidence"
    ]["cases"]


@pytest.mark.parametrize(
    "source_settings, expected_explanations",
    [
        (("read_cutoff = 10000",), {"read_cutoff"}),
        (
            ("read_cutoff = 10000", "nextgraph_a = 2"),
            {"read_cutoff", "nextgraph_a"},
        ),
        (
            ("read_cutoff = 10000", "unrecognized_option = 2"),
            {"read_cutoff"},
        ),
    ],
)
def test_cross_method_case_and_native_config_explanation_survive_real_messages(
    source_settings, expected_explanations
):
    corpus = snapshot()
    corpus = corpus.model_copy(
        update={
            "papers": (
                paper("nd", "NextDenovo", 420e6, settings=source_settings),
                corpus.papers[1],
            )
        }
    )
    for purpose in ("method_choice", "parameters"):
        result = retrieve_assembly_cases(
            corpus, query(), purpose=purpose, selected_method=TARGET
        )
        assert result["evidence"]["cases"][0]["method"]["tool"] == "NextDenovo"
        assert result["evidence"]["cases"][0]["match"]["platform_differences"] == [
            "hifi",
            "ont",
        ]
    cat = load_parameter_catalog(TARGET)
    facts = CanonicalRecommendationFacts()
    ctx = AssemblyRecommendationContext(
        stage="assembly",
        assembly_route="hifi",
        read_type="pacbio_hifi",
        target_level="draft",
    )
    basis = build_parameter_basis_snapshot(
        method_id=TARGET,
        catalog=cat,
        facts=facts,
        context=ctx,
        knowledge=corpus,
        material_query=query(),
    )
    dto = build_parameter_request_dto(
        method_id=TARGET,
        catalog=cat,
        facts=facts,
        context=ctx,
        task="recommend_initial",
        basis_snapshot=basis,
    )
    messages = render_assembly_parameter_messages(dto)
    text = messages[1]["content"]
    assert "read_cutoff = 10000" in text
    assert "Minimum read length cutoff" in text
    assert "NextDenovo" in text
    assert "read_cutoff" not in dto["adjustable_parameters"]
    assert "/private/current" not in text
    assert "retrieval_trace" not in text and "knowledge_sha256" not in text
    assert json.loads(text) == dto
    model_case = dto["knowledge_evidence"]["cases"][0]
    audit_case = basis["knowledge_evidence"]["cases"][0]
    assert model_case["relation"] == "cross_method"
    assert model_case["target_method"] == "hifiasm"
    source = model_case["source_method"]
    assert source["settings"] == list(source_settings)
    assert set(source["setting_explanations"]["settings"]) == expected_explanations
    assert set(audit_case["setting_explanations"]["settings"]) == expected_explanations
    # Unused NextDenovo descriptions stay out of the rendered request, even
    # though the internal software catalog contains them. The target catalog
    # remains separate and is not reduced to historical case settings.
    for native, spec in corpus.software["assembly.candidate.nextdenovo"][
        "parameters"
    ].items():
        if native not in expected_explanations:
            assert spec["meaning"] not in json.dumps(source)
    assert "NextDenovo work directory." not in text
    assert "Input FOFN path used in run.cfg." not in text
    assert expected_explanations.isdisjoint(dto["adjustable_parameters"])
    assert "purge_level" in dto["adjustable_parameters"]
    assert "method" not in model_case and "content" not in model_case
    assert audit_case["content"] == corpus.papers[0].cases[0].content
    assert audit_case["observations"] == [observation(420e6).model_dump(mode="json")]
    assert "do not directly map" in text
    assert "facts" not in dto["material_context"]
    assert "observations" not in dto["material_context"]
    assert "read_binding" not in dto["material_context"]
    with pytest.raises(ValueError, match="not cataloged as L2-adjustable"):
        resolve_parameters(
            cat,
            l0_executor_values={},
            snapshot=resolver_fixtures._assembly_snapshot(TARGET),
            approved_l2={"read_cutoff": 10000},
        )
    with pytest.raises(ValueError, match="pre-v5"):
        build_parameter_request_dto(
            method_id=TARGET,
            catalog=cat,
            facts=facts,
            context=ctx,
            task="recommend_initial",
            basis_snapshot={**basis, "consumer_version": 3},
        )


def test_missing_and_zero_measurements_defaults_and_whole_card_budgets():
    corpus = snapshot()
    assert (
        retrieve_assembly_cases(
            corpus, {}, purpose="parameters", selected_method=TARGET
        )["evidence"]["cases"]
        == []
    )
    assert observation(0).value == 0
    with pytest.raises(ValidationError):
        observation(float("nan"))
    tiny = retrieve_assembly_cases(
        corpus, query(), purpose="parameters", selected_method=TARGET, max_chars=256
    )
    assert tiny["evidence"]["cases"] == []
    assert {r["decision"] for r in tiny["trace"]} == {"whole_case_budget"}
    missing_settings = paper("m", "NextDenovo", 420e6)
    result = retrieve_assembly_cases(
        corpus.model_copy(update={"papers": (missing_settings,)}),
        query(),
        purpose="parameters",
        selected_method=TARGET,
    )
    assert result["evidence"]["cases"][0]["method"]["settings"] == []


def test_task_projections_allowlist_fields_without_mutating_full_evidence():
    case = retrieve_assembly_cases(
        snapshot(), query(), purpose="parameters", selected_method=TARGET
    )["evidence"]["cases"][0]
    case["method"]["version"] = "2.5"
    case["method"]["mode"] = "ont"
    case["route_roles"] = ["primary", "benchmark"]
    case["content"].update(
        samples=[{"name": "species A", "context": ["diploid", "2n=34"]}],
        sample_context="diploid",
        outcomes=[
            {
                "metric": "BUSCO",
                "value": "98%",
                "objects": ["final scaffold"],
                "basis": "combined",
                "context": ["after scaffolding"],
                "assessment": "BUSCO v5, lineage A",
                "evidence": ["AUDIT_ONLY_LOCATOR"],
            }
        ],
        gaps=[
            {
                "kind": "conflict",
                "issue": "version conflict",
                "leads": ["AUDIT_ONLY_LEAD"],
            }
        ],
        normalization_notes={"coverage": "combined coverage cannot be split"},
        evidence=["AUDIT_ONLY_EVIDENCE"],
    )
    case["future_audit_field"] = "AUDIT_ONLY_FUTURE"
    case["content"]["future_field"] = "AUDIT_ONLY_CONTENT"
    case["setting_explanations"]["settings"]["read_cutoff"]["future_metadata"] = (
        "AUDIT_ONLY_SEMANTICS"
    )
    original = deepcopy(case)
    method = project_method_choice_case(case)
    parameter = project_parameter_case(case, target_method="hifiasm")
    assert case == original
    assert set(method) == {
        "case_id",
        "method",
        "operation",
        "material",
        "execution_status",
        "route_role",
        "outcomes",
        "match",
        "limitations",
    }
    assert set(parameter) == (set(method) - {"method"}) | {
        "source_method",
        "target_method",
        "relation",
    }
    assert method["material"]["sample_traits"] == ["species A", "diploid", "2n=34"]
    assert method["material"]["observations"][0]["value"] == 420e6
    assert method["route_role"] == ["primary", "benchmark"]
    assert method["method"] == {"tool": "NextDenovo", "version": "2.5", "mode": "ont"}
    assert "settings" not in method["method"]
    assert parameter["relation"] == "cross_method"
    assert "Minimum read length cutoff" in json.dumps(parameter)
    assert parameter["outcomes"] == method["outcomes"]
    assert method["outcomes"][0]["basis"] == "combined"
    assert method["outcomes"][0]["attribution"] == "unknown"
    assert method["outcomes"][0]["assessment"] == "BUSCO v5, lineage A"
    assert "version conflict" in method["limitations"]
    assert "AUDIT_ONLY" not in json.dumps([method, parameter])
    assert (
        project_parameter_case(case, target_method="nextdenovo")["relation"]
        == "same_tool"
    )
    with pytest.raises(ValueError, match="target tool"):
        project_parameter_case(case, target_method="")
    parameter["outcomes"][0]["objects"].append("mutated")
    parameter["source_method"]["settings"].append("mutated")
    assert case == original
    case["route_roles"] = []
    case["method"]["settings"] = []
    assert project_method_choice_case(case)["route_role"] == []
    assert (
        project_parameter_case(case, target_method="hifiasm")["source_method"][
            "settings"
        ]
        == []
    )


def test_projection_keeps_comparable_and_grouped_measurement_semantics():
    case = retrieve_assembly_cases(snapshot(), query(), purpose="method_choice")[
        "evidence"
    ]["cases"][0]
    case["observations"][0].update(
        name="coverage",
        value=0,
        unit="x",
        platform="ont",
        state="filtered",
        origin="derived",
        basis="filtered reads / estimated genome size",
    )
    case["observations"].append(
        {
            **case["observations"][0],
            "value": 50,
            "origin": "reported",
            "state": "raw",
        }
    )
    case["content"]["reported_measurements"] = [
        {
            "metric": "total coverage",
            "value": "100x",
            "objects": ["HiFi", "ONT"],
            "basis": "combined",
            "context": ["Cannot assign to each platform"],
            "evidence": ["AUDIT_ONLY"],
        }
    ]
    case["execution_status"] = "described_only"
    view = project_method_choice_case(case)
    assert view["execution_status"] == "described_only"
    grouped, derived, reported = view["material"]["observations"]
    assert grouped["basis"] == "combined" and grouped["objects"] == ["HiFi", "ONT"]
    assert "evidence" not in grouped
    assert derived["value"] == 0 and derived["state"] == "filtered"
    assert derived["platform"] == "ont" and derived["origin"] == "derived"
    assert reported["state"] == "raw" and reported["origin"] == "reported"


def test_measurements_share_objects_without_losing_per_metric_qualifications():
    case = retrieve_assembly_cases(snapshot(), query(), purpose="method_choice")[
        "evidence"
    ]["cases"][0]
    label = "haplotype A; assembly; species A; individual; study"
    common = {"objects": [label], "basis": "each"}
    long_qualification = "Exact assessment scope. " * 30
    case["content"].update(
        samples=[{"name": "species A", "context": ["diploid"]}],
        outcomes=[
            {**common, "metric": "QV", "value": "55", "assessment": long_qualification},
            {
                **common,
                "metric": "QV",
                "value": "54",
                "assessment": "different evaluator",
            },
            {**common, "metric": "N50", "value": "10 Mb", "context": ["final output"]},
            {**common, "metric": "N50", "value": "10 Mb", "context": ["final output"]},
            {**common, "metric": "N50", "value": "9 Mb", "basis": "combined"},
        ],
        gaps=[
            {"kind": "scope_limit", "issue": "Downstream annotation inventory " * 50},
            {"kind": "conflict", "issue": long_qualification},
        ],
    )
    original = deepcopy(case)
    view = project_method_choice_case(case)
    first, combined = view["outcomes"]
    assert first["objects"] == ["haplotype A; assembly; individual; study"]
    assert "assessment" not in first
    assert first["metrics"] == [
        {"metric": "QV", "value": "55", "assessment": long_qualification},
        {"metric": "QV", "value": "54", "assessment": "different evaluator"},
        {"metric": "N50", "value": "10 Mb", "context": ["final output"]},
    ]
    assert combined["basis"] == "combined"
    assert long_qualification in view["limitations"]
    assert "Extraction has explicit scope limits" in json.dumps(view)
    assert "Downstream annotation inventory" not in json.dumps(view)
    assert case == original
    # Grouped samples must retain the per-object sample identity.
    case["content"]["samples"].append({"name": "species B", "context": []})
    assert project_method_choice_case(case)["outcomes"][0]["objects"] == [label]


@pytest.mark.parametrize("purpose", ["method_choice", "parameters"])
def test_budget_counts_whole_model_views_not_full_audit_records(purpose):
    corpus = snapshot().model_copy(update={"papers": (snapshot().papers[0],)})
    corpus.papers[0].cases[0].content["evidence"] = ["SOURCE_LOCATOR" * 5000]
    before = corpus.model_dump(mode="json")
    retrieved = retrieve_assembly_cases(
        corpus, query(), purpose=purpose, selected_method=TARGET
    )
    evidence = retrieved["evidence"]
    projected = project_assembly_evidence(evidence)
    budget = json_size(projected)
    assert json_size(evidence) > 32000
    assert budget < 32000
    exact = retrieve_assembly_cases(
        corpus, query(), purpose=purpose, selected_method=TARGET, max_chars=budget
    )
    assert exact["evidence"] == evidence
    too_small = retrieve_assembly_cases(
        corpus, query(), purpose=purpose, selected_method=TARGET, max_chars=budget - 1
    )
    assert too_small["evidence"]["cases"] == []
    assert too_small["trace"][0]["decision"] == "whole_case_budget"
    assert corpus.model_dump(mode="json") == before


def test_same_configuration_does_not_count_as_independent_evidence():
    original = paper("p", "NextDenovo", 420e6)
    case = original.cases[0]
    copies = original.model_copy(
        update={"cases": (case, case.model_copy(update={"case_id": "p:second"}))}
    )
    result = retrieve_assembly_cases(
        snapshot().model_copy(update={"papers": (copies,)}),
        query(),
        purpose="method_choice",
    )
    assert len(result["evidence"]["cases"]) == 1


def test_material_query_never_assigns_mixed_qc_to_hifi():
    state = {
        "workflow_steps": [{"stage": "read_qc", "step_id": "qc"}],
        "workflow_step_index": 1,
        "shared_parameters": {"estimated_genome_size": 1000},
        "stage_outputs": {
            "read_qc": {
                "summary": {"total_bases": 100000, "n50": 100},
                "raw_long_reads_paths": ["/a.fastq", "/b.fastq"],
                "input_member_platforms": ["pacbio_hifi", "ont"],
            }
        },
    }
    assembly = {"hifi_reads_ref": "/a.fastq", "ont_reads_ref": "/b.fastq"}
    mixed = build_material_query(state, assembly)
    assert (
        build_material_query(state, {**assembly, "target_level": "draft"})["text"]
        == mixed["text"]
    )
    assert all(
        o["name"] not in {"total_bases", "read_n50", "coverage"}
        for o in mixed["observations"]
    )
    qc = state["stage_outputs"]["read_qc"]
    qc["input_member_platforms"] = ["pacbio_hifi"]
    qc["raw_long_reads_paths"] = ["/a.fastq"]
    raw = build_material_query(state, assembly)
    bases = next(o for o in raw["observations"] if o["name"] == "total_bases")
    assert (bases["value"], bases["unit"], bases["platform"], bases["state"]) == (
        100000,
        "bp",
        "hifi",
        "raw",
    )
    qc["post_filter_summary"] = {"total_bases": 50000, "n50": 200}
    qc["filtered_long_reads_refs"] = ["/filtered.fastq"]
    measured = build_material_query(state, assembly)
    bases = next(o for o in measured["observations"] if o["name"] == "total_bases")
    assert (bases["value"], bases["state"]) == (50000, "filtered")
    case = paper("qc", "hifiasm", 1)
    case = case.model_copy(
        update={
            "cases": (
                case.cases[0].model_copy(
                    update={"observations": (Observation(**bases),)}
                ),
            )
        }
    )
    # No species text or genome traits: total bases alone reaches retrieval.
    retrieved = retrieve_assembly_cases(
        AssemblyKnowledge(software={}, papers=(case,)),
        {"observations": [bases]},
        purpose="method_choice",
    )
    assert retrieved["evidence"]["cases"][0]["match"]["matched_features"] == [
        "total_bases"
    ]
    coverage = next(o for o in measured["observations"] if o["name"] == "coverage")
    assert (coverage["value"], coverage["platform"], coverage["state"]) == (
        50,
        "hifi",
        "filtered",
    )
    assert "raw_coverage" not in measured["facts"]["sequencing"]
    qc["filtered_long_reads_refs"] = []
    assert all(
        o["name"] not in {"coverage", "total_bases"}
        for o in build_material_query(state, assembly)["observations"]
    )
    state["skip_reasons"] = {"qc": "skipped"}
    assert all(
        o["name"] not in {"coverage", "total_bases"}
        for o in build_material_query(state, assembly)["observations"]
    )


def test_case_loader_rejects_old_versions_duplicates_and_symlinks(tmp_path):
    file = tmp_path / "p.json"
    file.write_text(paper("p", "NextDenovo", 420e6).model_dump_json())
    assert len(load_case_papers(tmp_path)) == 1
    (tmp_path / "link.json").symlink_to(file)
    with pytest.raises(ValueError, match="symlink"):
        load_case_papers(tmp_path)
    (tmp_path / "link.json").unlink()
    file.write_text('{"schema_version": "recommendation-cases.pilot-v1"}')
    with pytest.raises(ValueError):
        load_case_papers(tmp_path)
    with pytest.raises(ValueError):
        AssemblyKnowledge(
            software={},
            papers=(paper("p", "NextDenovo", 1), paper("p", "NextDenovo", 2)),
        )
    with pytest.raises(ValueError):
        RunConfig.model_validate({"assembly": {"casepack": {"root": "/old"}}})


def test_freeze_does_not_copy_or_require_case_directory_and_live_reads_refresh(
    tmp_path,
):
    paths, manifest, config, envs = frozen_fixtures.FrozenInputTests()._fixture(
        tmp_path
    )
    source = tmp_path / "cases"
    source.mkdir()
    file = source / "nd.json"
    file.write_text(
        paper(
            "nd", "NextDenovo", 420e6, settings=("read_cutoff = 10000",)
        ).model_dump_json()
    )
    config.write_text(
        json.dumps(
            {
                "requested_stages": [
                    {"stage": "assembly", "allow_local_execution": False}
                ],
                "assembly": {"case_source": str(source)},
            }
        )
    )
    prepared = prepare_session_inputs(manifest_draft=manifest, run_config_draft=config)
    assert not paths.run_dir.exists()
    file.unlink()
    source.rmdir()
    bundle = freeze_session_inputs(
        paths, prepared_inputs=prepared, config_path=envs, probe_environments=False
    )
    assert set(bundle.marker.files) == {
        "input_manifest",
        "run_config",
        "dependency_manifest",
    }
    assert not (paths.control_dir / "assembly_knowledge.json").exists()
    assert validate_frozen_inputs(paths).run_config.assembly.case_source == str(source)
    from app.entrypoints.run import _stage_seeds_from_run_config

    state = {
        "data_dir": str(paths.run_dir),
        "session_id": paths.session_id,
        "stage_seeds": _stage_seeds_from_run_config(
            bundle.run_config, shared_parameters=None
        ),
    }
    with pytest.raises(FileNotFoundError):
        current_knowledge(state)
    source.mkdir()
    file.write_text(
        paper(
            "nd", "NextDenovo", 420e6, settings=("read_cutoff = 8000",)
        ).model_dump_json()
    )
    assert current_knowledge(state).papers[0].cases[0].method.settings == (
        "read_cutoff = 8000",
    )
    file.write_text(
        paper(
            "nd", "NextDenovo", 420e6, settings=("read_cutoff = 9000",)
        ).model_dump_json()
    )
    assert current_knowledge(state).papers[0].cases[0].method.settings == (
        "read_cutoff = 9000",
    )
    assert not (paths.control_dir / "assembly_knowledge.json").exists()


def test_method_request_persists_shared_query_and_cross_method_basis(
    tmp_path, monkeypatch
):
    corpus = snapshot()
    monkeypatch.setattr(
        "app.knowledge.assembly_runtime.current_knowledge", lambda _: corpus
    )
    reads = tmp_path / "reads.fastq"
    reads.write_text("@r\nACGT\n+\n!!!!\n")
    state = route_fixtures.InputProfilerTests()._state(
        str(tmp_path),
        assets=[
            {
                "asset_id": "ont",
                "asset_type": "clean_long_reads",
                "path": str(reads),
                "platform": "ont",
            }
        ],
    )
    state["shared_parameters"] = {"estimated_genome_size": 420000000, "platform": "ont"}
    resolved = {**state, **resolve_extended_inputs(state)}
    captured = []

    class Model:
        def invoke(self, messages):
            captured.extend(messages)
            return json.dumps(
                {
                    "recommended_route": "ont",
                    "recommended_read_type": "ont_raw",
                    "recommended_method": "assembly.candidate.nextdenovo",
                    "reasoning": "Matched sample and compatible reads",
                    "uncertainty_notes": ["No paired performance guarantee"],
                }
            )

    monkeypatch.setattr(
        "app.stages.assembly.decision.build_chat_model", lambda: Model()
    )
    update = infer_input_profile(resolved)
    assembly = update["stage_outputs"]["assembly"]
    payload = json.loads(captured[1]["content"])
    for route in payload["rules"]["routes"].values():
        assert set(route) == {"description"}
    assert set(payload["rules"]["routes"]) == {"cyclone", "ont", "hifi"}
    assert payload["knowledge_evidence"]["cases"][0]["method"]["tool"] == "NextDenovo"
    assert "read_cutoff = 10000" not in captured[1]["content"]
    assert "setting_explanations" not in captured[1]["content"]
    assert "content" not in payload["knowledge_evidence"]["cases"][0]
    assert str(tmp_path) not in captured[1]["content"]
    assert "retrieval_trace" not in payload
    state = {**resolved, **update}
    frozen_query = initial_material_query(state, assembly)
    assert (
        frozen_query["facts"]["genome"]["effective_genome_size"]["value"] == 420000000
    )
    state["shared_parameters"] = {"estimated_genome_size": 999999}
    assert initial_material_query(state, assembly) == frozen_query
    request_path = route_fixtures.InputProfilerTests()._ref_path(
        state, assembly["assembly_method_model_request_ref"]
    )
    record = json.loads(request_path.read_text())
    assert record["retrieval_trace"] and "knowledge_sha256" not in record
    assert record["consumer_version"] == 5
    assert record["knowledge_evidence"]["cases"][0]["method"]["settings"] == [
        "read_cutoff = 10000"
    ]
    assert (
        record["knowledge_evidence"]["cases"][0]["content"]
        == corpus.papers[0].cases[0].content
    )
    assert (
        assembly["recommended_assembly_selection"]["primary_method"]
        == "assembly.candidate.nextdenovo"
    )
    original_request = request_path.read_bytes()
    monkeypatch.setattr(
        "app.knowledge.assembly_runtime.current_knowledge",
        lambda _: pytest.fail("same recorded method request reloaded live cases"),
    )
    resumed = infer_input_profile(resolved)
    assert (
        resumed["stage_outputs"]["assembly"]["assembly_method_model_request_ref"]
        == assembly["assembly_method_model_request_ref"]
    )
    assert request_path.read_bytes() == original_request


def test_parameter_retry_reads_live_cases_but_recovery_keeps_recorded_request(
    tmp_path, monkeypatch
):
    from app.stages.assembly.parameters.workflow import (
        execution_retry_gate,
        recommend_assembly_parameters,
    )

    state = attempt_fixtures.ParameterRecommendationNodeTests()._state(str(tmp_path))
    attempt = attempt_fixtures._persist_parameter_attempt(
        state,
        method_id="assembly.candidate.hifiasm_ont",
        recommended_params={"purge_level": 1},
        status="execution_failed",
        execution_result={"return_code": 17, "status": "failed"},
    )
    state["stage_outputs"]["assembly"].update(
        status="failed", current_parameter_attempt_id=0, parameter_attempts=[attempt]
    )
    source = tmp_path / "live-cases"
    source.mkdir()
    case_file = source / "case.json"
    case_file.write_text(
        paper(
            "nd", "NextDenovo", 420e6, settings=("read_cutoff = 10000",)
        ).model_dump_json()
    )
    state["stage_seeds"]["assembly"] = {"case_source": str(source)}
    state["stage_outputs"] = execution_retry_gate(state)["stage_outputs"]
    model = attempt_fixtures._FakeModel(
        attempt_fixtures._complete_hifiasm_ont_response(purge_level=2)
    )
    monkeypatch.setattr(
        "app.llm.parameters.assembly.recommender.build_chat_model", lambda: model
    )
    # The old attempt's fixture has no cases; new retry must not inherit it.
    result = recommend_assembly_parameters(state)
    assert result["stage_outputs"]["assembly"]["status"] == "blocked_human"
    original_prompt = model.messages[1]["content"]
    assert "read_cutoff = 10000" in original_prompt
    case_file.unlink()
    for function in (
        "render_assembly_parameter_messages",
        "build_parameter_request_dto",
        "parameter_prompt_bundle_hash",
        "invoke_assembly_parameter_model",
    ):
        monkeypatch.setattr(
            f"app.stages.assembly.parameters.workflow.{function}",
            lambda *args, **kwargs: pytest.fail("same attempt rebuilt prompt"),
        )
    monkeypatch.setattr(
        "app.knowledge.assembly_runtime.current_knowledge",
        lambda _: pytest.fail("same attempt reopened case directory"),
    )
    monkeypatch.setattr(
        "app.stages.assembly.parameters.basis.current_knowledge",
        lambda _: pytest.fail("same attempt reopened case directory"),
    )
    resumed = recommend_assembly_parameters(state)
    assert resumed["stage_outputs"]["assembly"]["status"] == "blocked_human"
    assert model.messages[1]["content"] == original_prompt


def test_initial_parameter_recovery_does_not_reload_current_catalog(
    tmp_path, monkeypatch
):
    from app.stages.assembly.parameters.workflow import recommend_assembly_parameters

    state = attempt_fixtures.ParameterRecommendationNodeTests()._state(str(tmp_path))
    model = attempt_fixtures._FakeModel(
        attempt_fixtures._complete_hifiasm_ont_response(purge_level=2)
    )
    monkeypatch.setattr(
        "app.llm.parameters.assembly.recommender.build_chat_model", lambda: model
    )
    first = recommend_assembly_parameters(state)
    assert first["stage_outputs"]["assembly"]["status"] == "blocked_human"
    for name in (
        "load_parameter_catalog",
        "render_assembly_parameter_messages",
        "build_parameter_request_dto",
        "invoke_assembly_parameter_model",
    ):
        monkeypatch.setattr(
            f"app.stages.assembly.parameters.workflow.{name}",
            lambda *args, **kwargs: pytest.fail(
                "recovery used current catalog or prompt"
            ),
        )
    replay = recommend_assembly_parameters(state)
    assert (
        replay["stage_outputs"]["assembly"]["current_parameter_recommendation"]
        == first["stage_outputs"]["assembly"]["current_parameter_recommendation"]
    )


@pytest.mark.parametrize("binding", ["basis_sha256", "input_snapshot_sha256"])
def test_parameter_recovery_rejects_unbound_request(tmp_path, monkeypatch, binding):
    from app.stages.assembly.parameters import workflow

    state = attempt_fixtures.ParameterRecommendationNodeTests()._state(str(tmp_path))
    model = attempt_fixtures._FakeModel(
        attempt_fixtures._complete_hifiasm_ont_response(purge_level=2)
    )
    monkeypatch.setattr(
        "app.llm.parameters.assembly.recommender.build_chat_model", lambda: model
    )
    write = workflow.write_artifact

    def mismatched_request(state, identifier, filename, payload, **kwargs):
        if filename == "model_request.json":
            payload = {**payload, binding: "wrong"}
        return write(state, identifier, filename, payload, **kwargs)

    monkeypatch.setattr(workflow, "write_artifact", mismatched_request)
    assert (
        workflow.recommend_assembly_parameters(state)["stage_outputs"]["assembly"][
            "status"
        ]
        == "blocked_human"
    )
    monkeypatch.setattr(
        workflow,
        "invoke_assembly_parameter_model",
        lambda *a, **k: pytest.fail("unbound request invoked model"),
    )
    replay = workflow.recommend_assembly_parameters(state)
    assert "evidence binding mismatch" in str(replay)


@pytest.mark.parametrize(
    "invalid",
    [
        {"schema_version": 2},
        {"consumer_version": 4},
        {"knowledge_evidence": []},
        {"knowledge_evidence": {"purpose": "parameters", "target_method": None}},
        {
            "knowledge_evidence": {
                "purpose": "method_choice",
                "target_method": "hifiasm",
            }
        },
        {
            "knowledge_evidence": {
                "purpose": "parameters",
                "target_method": "NextDenovo",
            }
        },
    ],
)
def test_parameter_basis_guard_rejects_bad_schema_and_evidence(invalid):
    catalog = load_parameter_catalog(TARGET)
    basis = {
        "schema_version": 3,
        "consumer_version": 5,
        "method_id": TARGET,
        "knowledge_evidence": {"purpose": "parameters", "target_method": catalog.tool},
    }
    with pytest.raises(ValueError):
        validate_parameter_basis(
            method_id=TARGET, catalog=catalog, basis_snapshot={**basis, **invalid}
        )


def projection_fixture():
    graph = {
        "schema_version": "assembly-extraction.v5",
        "paper": {"title": "Synthetic"},
        "sources": [],
        "samples": [{"id": "s", "name": "sample"}],
        "states": [
            {
                "id": "r",
                "kind": "reads",
                "sample_ids": ["s"],
                "sample_scope": "individual",
            },
            {"id": "a", "kind": "contigs", "sample_ids": ["s"]},
        ],
        "methods": [
            {
                "id": "m",
                "tool": "NextDenovo",
                "settings": ["read_cutoff = 10000", "unknown_option=2"],
            }
        ],
        "steps": [
            {
                "id": "st",
                "name": "assemble",
                "method_id": "m",
                "sample_ids": ["s"],
                "inputs": [{"state_id": "r", "role": "target"}],
                "output_state_ids": ["a"],
                "execution_status": "performed",
            }
        ],
        "routes": [],
        "measurements": [
            {
                "id": "q",
                "state_ids": ["r", "a"],
                "metric": "coverage",
                "value": "30x",
                "basis": "combined",
            }
        ],
        "gaps": [],
    }
    selection = [
        {
            "paper": "p",
            "stage": "assembly",
            "task": "contig_assembly",
            "step": "st",
            "inputs": {"r": "ont"},
            "features": {"ont_coverage": {"measurement": "q", "unit": "x"}},
        }
    ]
    return graph, selection


def test_explicit_graph_projection_retains_settings_and_combined_measurements():
    graph, selection = projection_fixture()
    result = project_paper("p", graph, selection)
    assert result.cases[0].method.settings == (
        "read_cutoff = 10000",
        "unknown_option=2",
    )
    assert result.cases[0].observations == ()
    assert result.cases[0].content["reported_measurements"][0]["basis"] == "combined"
    summary = result.cases[0].content
    assert isinstance(summary["inputs"][0]["material"], str)
    assert all(isinstance(s, str) for s in summary["outputs"])
    assert "measurement_materials" not in summary and "assessments" not in summary
    assert "state_ids" not in summary["reported_measurements"][0]
    for bad_content in (
        {**summary, "measurement_materials": graph["states"]},
        {**summary, "inputs": [{"role": "target", "material": graph["states"][0]}]},
    ):
        with pytest.raises(ValidationError):
            AssemblyCase.model_validate(
                {**result.cases[0].model_dump(), "content": bad_content}
            )


@pytest.mark.parametrize(
    "objects, expected",
    [
        (["a"], "direct_output"),
        (["final"], "downstream_context"),
        (["a", "final"], "mixed_context"),
        (["final", "r"], "mixed_context"),
    ],
)
def test_assembly_phasing_inputs_and_output_attribution(objects, expected):
    graph, selection = projection_fixture()
    graph["states"].extend(
        [
            {
                "id": "hic",
                "name": "trimmed Hi-C reads",
                "kind": "reads",
                "sample_ids": ["s"],
            },
            {"id": "final", "kind": "scaffolds", "sample_ids": ["s"]},
        ]
    )
    graph["steps"][0]["name"] = "assemble with Hi-C phasing"
    graph["steps"][0]["inputs"].append(
        {"state_id": "hic", "role": "support", "use": "haplotype phasing"}
    )
    graph["steps"].append(
        {
            "id": "scaffold",
            "name": "scaffold",
            "inputs": [{"state_id": "a", "role": "target"}],
            "output_state_ids": ["final"],
        }
    )
    graph["measurements"].append(
        {
            "id": "result",
            "state_ids": objects,
            "metric": "QV",
            "value": "50",
            "basis": "combined",
        }
    )
    selection[0]["outcomes"] = ["result"]
    selection[0]["inputs"]["hic"] = "hic"
    projected = project_paper("p", graph, selection)
    corpus = snapshot().model_copy(update={"papers": (projected,)})
    audit = retrieve_assembly_cases(
        corpus, {"text": "phasing"}, purpose="method_choice"
    )["evidence"]["cases"][0]
    dto = project_method_choice_case(audit)
    assert dto["operation"]["action"] == "assemble with Hi-C phasing"
    assert dto["operation"]["inputs"][1]["role"] == "support"
    assert dto["operation"]["inputs"][1]["use"] == "haplotype phasing"
    assert dto["operation"]["outputs"] == ["a; contigs; sample"]
    assert dto["outcomes"][0]["attribution"] == expected
    assert "settings" not in dto["method"]
    assert "mode" not in dto["method"]  # Never invent a mode from a phrase.
    assert projected.cases[0].method.settings == tuple(graph["methods"][0]["settings"])
    # Using contigs as a reference/support does not make outputs their descendants.
    if objects == ["final"]:
        graph["steps"][1]["inputs"][0]["role"] = "reference"
        with pytest.raises(ValueError, match="descendants"):
            project_paper("p", graph, selection)


@pytest.mark.parametrize("raw", ["2", "2 count", "2.0"])
def test_explicit_ploidy_count_projection_participates_in_retrieval(raw):
    graph, selection = projection_fixture()
    graph["states"].append(
        {
            "id": "g",
            "kind": "biological_genome",
            "sample_ids": ["s"],
            "sample_scope": "individual",
            "origin": "study",
        }
    )
    graph["measurements"].append(
        {
            "id": "pl",
            "state_ids": ["g"],
            "metric": "ploidy",
            "value": raw,
            "basis": "each",
        }
    )
    selection[0]["features"] = {"ploidy": {"measurement": "pl", "unit": "count"}}
    projected = project_paper("p", graph, selection)
    ploidy = projected.cases[0].observations[0]
    assert (ploidy.name, ploidy.value, ploidy.unit) == ("ploidy", 2, "count")
    current = build_material_query({"shared_parameters": {"ploidy": 2}}, {})
    result = retrieve_assembly_cases(
        AssemblyKnowledge(software={}, papers=(projected,)),
        current,
        purpose="method_choice",
    )
    assert result["evidence"]["cases"][0]["match"]["matched_features"] == ["ploidy"]
    assert result["evidence"]["cases"][0]["match"]["comparisons"][0]["similarity"] == 1
    for unsupported in ("diploid", "2n=34", "2x", "2–4", "two"):
        graph["measurements"][-1]["value"] = unsupported
        assert not project_paper("p", graph, selection).cases[0].observations
