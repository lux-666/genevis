from __future__ import annotations

import re
from hashlib import sha256

import pytest
from pydantic import ValidationError

from app.domains.parameter_recommendation.facts import build_canonical_facts
from app.schemas.artifacts import ArtifactRef
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalRecommendationFacts,
    CanonicalSpeciesFacts,
    ParameterRecommendationManifestV3,
    RecommendationDependencyRefs,
    RecommendationInputSnapshotV3,
    T2TMotifResolutionManifestV2,
    T2TRecommendationContext,
)


def _ref(name: str, artifact_type: str = "test.artifact") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"artifact:{name}",
        path_ref=f"artifacts/test/{name}.json",
        sha256=sha256(name.encode("utf-8")).hexdigest(),
        size_bytes=1,
        artifact_type=artifact_type,
    )


def _manifest_refs() -> dict[str, dict]:
    return {
        "input_snapshot_ref": _ref("a").model_dump(mode="json"),
        "basis_snapshot_ref": _ref("b").model_dump(mode="json"),
        "model_request_ref": _ref("c").model_dump(mode="json"),
        "model_response_ref": _ref("d").model_dump(mode="json"),
        "response_parse_ref": _ref("e").model_dump(mode="json"),
        "recommendation_ref": _ref("f").model_dump(mode="json"),
    }


def test_input_snapshot_requires_explicit_schema_3() -> None:
    payload = {
        "snapshot_kind": "parameter_recommendation",
        "method_id": "assembly.candidate.flye",
        "task": "recommend_initial",
        "facts": CanonicalRecommendationFacts().model_dump(mode="json"),
        "stage_context": AssemblyRecommendationContext(
            stage="assembly",
            assembly_route="ont",
            read_type="ont_raw",
            target_level="draft",
        ).model_dump(mode="json"),
        "dependency_refs": RecommendationDependencyRefs().model_dump(mode="json"),
    }

    with pytest.raises(ValidationError):
        RecommendationInputSnapshotV3.model_validate(payload)

    snapshot = RecommendationInputSnapshotV3.model_validate(
        {"schema_version": 3, **payload}
    )
    assert snapshot.schema_version == 3


def test_shared_genome_size_is_a_run_config_fact_without_survey_dependency() -> None:
    facts = build_canonical_facts(
        shared_parameters={"estimated_genome_size": 2_000_000},
        read_qc_output=None,
        genome_survey_result=None,
    )

    assert facts.genome.effective_genome_size.value == 2_000_000
    assert facts.genome.effective_genome_size.origin.model_dump() == {
        "kind": "provided",
        "producer": "run_config",
    }
    snapshot = RecommendationInputSnapshotV3(
        schema_version=3,
        method_id="assembly.candidate.flye",
        task="recommend_initial",
        facts=facts,
        stage_context=AssemblyRecommendationContext(
            stage="assembly",
            assembly_route="ont",
            read_type="ont_raw",
            target_level="draft",
        ),
    )
    assert snapshot.dependency_refs.fact_source_refs == {}


def test_canonical_facts_preserve_absent_values_and_production_numeric_shapes() -> None:
    facts = build_canonical_facts(
        shared_parameters={"taxon_id": 9606, "ploidy": None},
        read_qc_output={
            "summary": {
                "num_seqs": 10.0,
                "n50": None,
                "q20_percent": 0,
            }
        },
        genome_survey_result={
            "confidence": None,
            "estimates": {
                "genome_size": 2_000_000.0,
                "heterozygosity": None,
                "repeat_fraction": 1,
            },
        },
        asset_flags={"has_hifi": None, "has_ont": False},
    )

    assert facts.species.taxon_id == "9606"
    assert facts.species.ploidy is None
    assert facts.reads.read_count.value == 10
    assert facts.reads.read_n50 is None
    assert facts.reads.q20_fraction.value == 0
    assert facts.genome.effective_genome_size.value == 2_000_000
    assert facts.genome.heterozygosity is None
    assert facts.genome.repeat_fraction.value == 1
    assert facts.genome.estimate_confidence is None
    assert facts.asset_presence.has_hifi is None
    assert facts.asset_presence.has_ont is False


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        (
            {"read_qc_output": {"summary": {"num_seqs": True}}},
            "read_qc.summary.num_seqs",
        ),
        (
            {"read_qc_output": {"summary": {"total_bases": float("inf")}}},
            "read_qc.summary.total_bases",
        ),
        (
            {"read_qc_output": {"summary": {"avg_len": 0}}},
            "read_qc.summary.avg_len",
        ),
        (
            {"read_qc_output": {"summary": {"n50": 3.5}}},
            "read_qc.summary.n50",
        ),
        (
            {"read_qc_output": {"summary": {"q20_percent": 100.1}}},
            "read_qc.summary.q20_percent",
        ),
        (
            {"genome_survey_result": {"estimates": {"heterozygosity": -0.1}}},
            "genome_survey.estimates.heterozygosity",
        ),
        (
            {"genome_survey_result": {"estimates": {"repeat_fraction": float("nan")}}},
            "genome_survey.estimates.repeat_fraction",
        ),
        (
            {"shared_parameters": {"estimated_genome_size": -1}},
            "shared_parameters.estimated_genome_size",
        ),
        (
            {"shared_parameters": {"estimated_genome_size": 10**1000}},
            "shared_parameters.estimated_genome_size",
        ),
        (
            {"shared_parameters": {"ploidy": 2.5}},
            "shared_parameters.ploidy",
        ),
    ],
)
def test_canonical_facts_reject_malformed_present_numeric_values(
    overrides: dict,
    field: str,
) -> None:
    inputs = {
        "shared_parameters": {},
        "read_qc_output": None,
        "genome_survey_result": None,
    }
    inputs.update(overrides)

    with pytest.raises(ValueError, match=re.escape(field)):
        build_canonical_facts(**inputs)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        (
            {"read_qc_output": {"summary": []}},
            "read_qc.summary",
        ),
        (
            {"genome_survey_result": {"estimates": None}},
            "genome_survey.estimates",
        ),
        (
            {"genome_survey_result": {"measured_estimates": "invalid"}},
            "genome_survey.measured_estimates",
        ),
        (
            {"genome_survey_result": {"provided_overrides": 1}},
            "genome_survey.provided_overrides",
        ),
        (
            {"genome_survey_result": {"estimate_sources": []}},
            "genome_survey.estimate_sources",
        ),
    ],
)
def test_canonical_facts_reject_malformed_present_nested_mappings(
    overrides: dict,
    field: str,
) -> None:
    inputs = {
        "shared_parameters": {},
        "read_qc_output": None,
        "genome_survey_result": None,
    }
    inputs.update(overrides)

    with pytest.raises(ValueError, match=re.escape(field)):
        build_canonical_facts(**inputs)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        (
            {"asset_flags": {"has_hifi": 1}},
            "asset_flags.has_hifi",
        ),
        (
            {"genome_survey_result": {"confidence": "medium"}},
            "genome_survey.confidence",
        ),
        (
            {
                "genome_survey_result": {"confidence": []},
            },
            "genome_survey.confidence",
        ),
        (
            {
                "shared_parameters": {"estimated_genome_size": 2_000_000},
                "genome_survey_result": {"estimate_sources": {"genome_size": "legacy"}},
            },
            "genome_survey.estimate_sources.genome_size",
        ),
    ],
)
def test_canonical_facts_reject_invalid_flags_and_enums(
    overrides: dict,
    field: str,
) -> None:
    inputs = {
        "shared_parameters": {},
        "read_qc_output": None,
        "genome_survey_result": None,
    }
    inputs.update(overrides)

    with pytest.raises(ValueError, match=re.escape(field)):
        build_canonical_facts(**inputs)


def test_retry_task_requires_typed_retry_context() -> None:
    with pytest.raises(ValidationError, match="typed retry context"):
        RecommendationInputSnapshotV3(
            schema_version=3,
            method_id="assembly.candidate.flye",
            task="retry_after_execution_failure",
            facts=CanonicalRecommendationFacts(),
            stage_context=AssemblyRecommendationContext(
                stage="assembly",
                assembly_route="ont",
                read_type="ont_raw",
                target_level="draft",
            ),
        )


def test_t2t_snapshot_requires_motif_ref_and_exact_long_read_dependencies() -> None:
    context = T2TRecommendationContext(
        stage="t2t",
        resolved_telomere_motif="TTAGGG",
        motif_source="provided_species_fact",
        long_read_asset_count=2,
    )
    with pytest.raises(ValidationError, match="motif_resolution_ref"):
        RecommendationInputSnapshotV3(
            schema_version=3,
            method_id="t2t.alignment.minimap2",
            task="recommend_initial",
            facts=CanonicalRecommendationFacts(),
            stage_context=context,
        )

    snapshot = RecommendationInputSnapshotV3(
        schema_version=3,
        method_id="t2t.alignment.minimap2",
        task="recommend_initial",
        facts=CanonicalRecommendationFacts(),
        stage_context=context,
        dependency_refs=RecommendationDependencyRefs(
            stage_input_refs=(_ref("assembly"), _ref("a"), _ref("b")),
            motif_resolution_ref=_ref("m"),
        ),
    )
    assert len(snapshot.dependency_refs.stage_input_refs) == 3


def test_t2t_snapshot_accepts_one_indexed_registry_for_long_read_inputs() -> None:
    snapshot = RecommendationInputSnapshotV3(
        schema_version=3,
        method_id="t2t.alignment.minimap2",
        task="recommend_initial",
        facts=CanonicalRecommendationFacts(),
        stage_context=T2TRecommendationContext(
            stage="t2t",
            resolved_telomere_motif="TTAGGG",
            motif_source="provided_species_fact",
            long_read_asset_count=3,
        ),
        dependency_refs=RecommendationDependencyRefs(
            stage_input_refs=(
                _ref("assembly", "input.assembly_fasta"),
                _ref("registry", "asset_registry"),
            ),
            motif_resolution_ref=_ref("motif"),
        ),
    )

    assert (
        snapshot.dependency_refs.stage_input_refs[1].artifact_type == "asset_registry"
    )


@pytest.mark.parametrize("motif", ["X", "not dna", "ACGTN"])
def test_canonical_motif_fields_reject_non_dna_values(motif: str) -> None:
    with pytest.raises(ValidationError):
        CanonicalSpeciesFacts(supplied_telomere_motif=motif)

    with pytest.raises(ValidationError):
        T2TRecommendationContext(
            stage="t2t",
            resolved_telomere_motif=motif,
            motif_source="provided_species_fact",
            long_read_asset_count=1,
        )


def test_recommendation_manifest_rejects_old_ref_key() -> None:
    payload = {
        "schema_version": 3,
        "recommendation_contract": "parameter_recommendation_v1",
        "stage": "assembly",
        "method_id": "assembly.candidate.flye",
        "task": "recommend_initial",
        "recommendation_id": 0,
        "status": "pending_approval",
        "refs": _manifest_refs(),
    }
    manifest = ParameterRecommendationManifestV3.model_validate(payload)
    assert manifest.refs.input_snapshot_ref.path_ref.endswith("a.json")

    old_refs = _manifest_refs()
    old_refs["input_profile_ref"] = old_refs.pop("input_snapshot_ref")
    with pytest.raises(ValidationError):
        ParameterRecommendationManifestV3.model_validate({**payload, "refs": old_refs})


def test_motif_manifest_enforces_conditional_model_refs() -> None:
    provided = T2TMotifResolutionManifestV2(
        schema_version=2,
        resolution_contract="t2t_telomere_motif_v1",
        resolver_id="t2t.telomere_motif_resolver",
        resolution_mode="provided_species_fact",
        status="resolved",
        refs={
            "input_snapshot_ref": _ref("a"),
            "resolution_ref": _ref("r"),
        },
    )
    assert provided.refs.model_request_ref is None

    with pytest.raises(ValidationError, match="must not contain model refs"):
        T2TMotifResolutionManifestV2.model_validate(
            {
                **provided.model_dump(mode="json"),
                "refs": {
                    **provided.refs.model_dump(mode="json"),
                    "model_request_ref": _ref("q").model_dump(mode="json"),
                },
            }
        )

    with pytest.raises(ValidationError, match="require all model refs"):
        T2TMotifResolutionManifestV2(
            schema_version=2,
            resolution_contract="t2t_telomere_motif_v1",
            resolver_id="t2t.telomere_motif_resolver",
            resolution_mode="llm_species_resolution",
            status="resolved",
            refs={
                "input_snapshot_ref": _ref("a"),
                "resolution_ref": _ref("r"),
            },
        )
