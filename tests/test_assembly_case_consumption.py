"""v5-derived evidence through retrieval, real DTO/rendering and frozen runs."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.domains.assembly.material_query import build_material_query
from app.domains.assembly.software_knowledge import capture_software_knowledge
from app.domains.parameter_recommendation.resolution import resolve_parameters
from app.knowledge.assembly_cases import (
    AssemblyCase,
    CasePaper,
    KnowledgeSnapshot,
    Observation,
    SourceMethod,
    load_case_papers,
)
from app.knowledge.assembly_retrieval import retrieve_assembly_cases
from app.knowledge.assembly_runtime import frozen_knowledge, initial_material_query
from app.knowledge.v5_projection import project_paper
from app.llm.parameters.assembly.prompt import render_assembly_parameter_messages
from app.llm.parameters.assembly.request import build_parameter_request_dto
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalRecommendationFacts,
)
from app.schemas.run_config import RunConfig
from app.stages.assembly.nodes import infer_input_profile, resolve_extended_inputs
from app.stages.assembly.parameters.basis import build_parameter_basis_snapshot
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.storage.frozen_inputs import (
    FrozenInputIntegrityError,
    freeze_session_inputs,
    prepare_session_inputs,
    validate_frozen_inputs,
)
from tests import test_frozen_inputs as frozen_fixtures
from tests import test_parameter_resolution_validation as resolver_fixtures
from tests import test_phase8_slice1 as route_fixtures

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
                    "background": "repeat-rich diploid genome",
                    "result": "assembly outcome, not parameter effect",
                },
            ),
        ),
    )


def snapshot() -> KnowledgeSnapshot:
    return KnowledgeSnapshot(
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


def test_cross_method_case_and_native_config_explanation_survive_real_messages():
    corpus = snapshot()
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
    assert dto["knowledge_evidence"] == basis["knowledge_evidence"]
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
        retrieve_assembly_cases(corpus, {}, purpose="parameters")["evidence"]["cases"]
        == []
    )
    assert observation(0).value == 0
    with pytest.raises(ValidationError):
        observation(float("nan"))
    tiny = retrieve_assembly_cases(corpus, query(), purpose="parameters", max_chars=256)
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
    assert all(o["name"] not in {"read_n50", "coverage"} for o in mixed["observations"])
    qc = state["stage_outputs"]["read_qc"]
    qc["input_member_platforms"] = ["pacbio_hifi"]
    qc["raw_long_reads_paths"] = ["/a.fastq"]
    qc["post_filter_summary"] = {"total_bases": 50000, "n50": 200}
    qc["filtered_long_reads_refs"] = ["/filtered.fastq"]
    measured = build_material_query(state, assembly)
    coverage = next(o for o in measured["observations"] if o["name"] == "coverage")
    assert (coverage["value"], coverage["platform"], coverage["state"]) == (
        50,
        "hifi",
        "filtered",
    )
    assert "raw_coverage" not in measured["facts"]["sequencing"]
    qc["filtered_long_reads_refs"] = []
    assert all(
        o["name"] != "coverage"
        for o in build_material_query(state, assembly)["observations"]
    )
    state["skip_reasons"] = {"qc": "skipped"}
    assert all(
        o["name"] != "coverage"
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
        KnowledgeSnapshot(
            software={},
            papers=(paper("p", "NextDenovo", 1), paper("p", "NextDenovo", 2)),
        )
    with pytest.raises(ValueError):
        RunConfig.model_validate({"assembly": {"casepack": {"root": "/old"}}})


def test_real_freeze_captures_contents_and_survives_source_removal(
    tmp_path, monkeypatch
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
    assert bundle.file_sha256("assembly_knowledge")
    assert validate_frozen_inputs(paths).run_config.assembly.case_source == str(source)
    state = {"data_dir": str(paths.run_dir), "session_id": paths.session_id}

    def fail_live_capture():
        raise AssertionError("frozen runtime reopened live software knowledge")

    monkeypatch.setattr(
        "app.knowledge.assembly_runtime.capture_software_knowledge", fail_live_capture
    )
    assert frozen_knowledge(state).papers[0].cases[0].method.settings == (
        "read_cutoff = 10000",
    )
    paths.assembly_knowledge_path.write_text("{}")
    with pytest.raises(FrozenInputIntegrityError, match="hash mismatch"):
        validate_frozen_inputs(paths)


def test_method_request_persists_shared_query_and_cross_method_basis(
    tmp_path, monkeypatch
):
    corpus = snapshot()
    monkeypatch.setattr(
        "app.knowledge.assembly_runtime.frozen_knowledge", lambda _: corpus
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
    assert payload["knowledge_evidence"]["cases"][0]["method"]["tool"] == "NextDenovo"
    assert "read_cutoff = 10000" in captured[1]["content"]
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
    assert record["retrieval_trace"] and record["knowledge_sha256"]
    assert record["consumer_version"] == 5
    assert (
        assembly["recommended_assembly_selection"]["primary_method"]
        == "assembly.candidate.nextdenovo"
    )


def test_explicit_graph_projection_retains_settings_and_combined_measurements():
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
    result = project_paper("p", graph, selection)
    assert result.cases[0].method.settings == (
        "read_cutoff = 10000",
        "unknown_option=2",
    )
    assert result.cases[0].observations == ()
    assert result.cases[0].content["reported_measurements"][0]["basis"] == "combined"
