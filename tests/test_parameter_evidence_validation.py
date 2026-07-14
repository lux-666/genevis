"""Tests for optional, auditable parameter evidence bases."""

from app.domains.assembly.profile_evidence import build_profile_evidence
from app.stages.assembly.parameter_validation import validate_parameter_evidence


def _with_profile(payload: dict, prompt_profile: dict) -> dict:
    payload = dict(payload)
    payload["prompt_profile"] = prompt_profile
    payload["observations"], payload["evidence_registry"] = build_profile_evidence(
        prompt_profile
    )
    return payload


def _evidence_id(payload: dict, source_path: str) -> str:
    return next(
        evidence_id
        for evidence_id, registered_path in payload["evidence_registry"].items()
        if registered_path == source_path
    )


def _payload() -> dict:
    prompt_profile = {"genome_survey": {"heterozygosity": 0.012}}
    return _with_profile({
        "task": "recommend_initial",
        "case_selection": {
            "retrieval_backend": "hifiasm_numeric",
            "selected_case_ids": ["case_a"],
        },
        "evidence_cases": [
            {
                "case_id": "case_a",
                "source_excerpts": [
                    {"span_id": "methods.rationale", "document": "methods", "quote": "Exact rationale."}
                ],
            }
        ],
    }, prompt_profile)


def test_profile_evidence_ids_are_stable_when_unrelated_facts_are_added():
    observations, registry = build_profile_evidence(
        {"assets": {"read_type": "ont", "has_ont": True}}
    )
    expanded_observations, expanded_registry = build_profile_evidence(
        {
            "sample": {"organism": "Arabidopsis thaliana"},
            "assets": {"has_ont": True, "read_type": "ont"},
        }
    )
    read_type_id = next(
        evidence_id
        for evidence_id, registered_path in registry.items()
        if registered_path == "assets.read_type"
    )
    expanded_read_type_id = next(
        evidence_id
        for evidence_id, registered_path in expanded_registry.items()
        if registered_path == "assets.read_type"
    )
    assert read_type_id == expanded_read_type_id
    assert read_type_id.startswith("pf_")
    assert len(read_type_id) == 13
    reordered_observations, reordered_registry = build_profile_evidence(
        {"assets": {"has_ont": True, "read_type": "ont"}}
    )
    assert observations == reordered_observations
    assert registry == reordered_registry
    assert any(item["id"] == read_type_id for item in expanded_observations)


def test_profile_fact_cites_opaque_observation_ids():
    payload = _with_profile(
        _payload(),
        {
            "assets": {
                "assembly_route": "ont",
                "read_type": "ont",
                "has_ont": True,
            },
            "genome_survey": {"heterozygosity": 0.012},
        },
    )

    evidence_ids = [
        _evidence_id(payload, source_path)
        for source_path in (
            "assets.assembly_route",
            "assets.read_type",
            "assets.has_ont",
        )
    ]
    result = validate_parameter_evidence(
        parameter_evidence={
            "dual_scaffold": {
                "bases": ["profile_fact", "catalog_default"],
                "evidence_ids": evidence_ids,
            }
        },
        recommended_params={"dual_scaffold": True},
        prompt_payload=payload,
    )
    assert result["valid"] is True, result["violations"]
    assert all("." not in observation["id"] for observation in payload["observations"])
    assert all("source_path" not in observation for observation in payload["observations"])


def test_profile_fact_rejects_ids_outside_closed_surface():
    payload = _payload()
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "profile_fact",
                "evidence_ids": ["pf_not_registered"],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=payload,
    )

    assert result["valid"] is False
    assert "absent from observations" in result["violations"][0]["reason"]


def test_fact_paths_are_rejected_instead_of_normalized():
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "profile_fact",
                "fact_paths": ["genome_survey.heterozygosity"],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=_payload(),
    )
    assert result["valid"] is False
    assert any("fact_paths is no longer supported" in item["reason"] for item in result["violations"])


def test_evidence_ids_require_profile_fact_basis():
    payload = _payload()
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "catalog_default",
                "evidence_ids": [_evidence_id(payload, "genome_survey.heterozygosity")],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=payload,
    )
    assert result["valid"] is False
    assert "require basis=profile_fact" in result["violations"][0]["reason"]


def test_observation_surface_is_authoritative_over_internal_registry():
    payload = _with_profile(_payload(), {
        "assets": {"has_ont": True},
        "genome_survey": {"heterozygosity": 0.012},
    })
    has_ont_id = _evidence_id(payload, "assets.has_ont")
    payload["observations"] = [
        item for item in payload["observations"] if item["id"] != has_ont_id
    ]
    result = validate_parameter_evidence(
        parameter_evidence={
            "dual_scaffold": {
                "basis": "profile_fact",
                "evidence_ids": [has_ont_id],
            }
        },
        recommended_params={"dual_scaffold": True},
        prompt_payload=payload,
    )
    assert result["valid"] is False
    assert "absent from observations" in result["violations"][0]["reason"]


def test_storage_locators_are_audited_but_not_observable_or_citable():
    payload = _with_profile(
        _payload(),
        {
            "assets": {
                "read_type": "ont",
                "input_files": ["/data/reads.fastq.gz"],
                "filename": "reads.fastq.gz",
                "reads_ref": "artifact://reads",
            },
            "previous_attempt": {
                "params": {"threads": 64},
                "stderr_path": "/tmp/tool.stderr",
            },
        },
    )
    locator_ids = {
        _evidence_id(payload, "assets.input_files"),
        _evidence_id(payload, "assets.filename"),
        _evidence_id(payload, "assets.reads_ref"),
        _evidence_id(payload, "previous_attempt.stderr_path"),
    }
    visible_ids = {item["id"] for item in payload["observations"]}
    assert not (locator_ids & visible_ids)
    previous_param_id = _evidence_id(payload, "previous_attempt.params.threads")
    assert previous_param_id in visible_ids
    assert next(
        item for item in payload["observations"] if item["id"] == previous_param_id
    ) == {
        "id": previous_param_id,
        "section": "previous_attempt",
        "group": "params",
        "name": "threads",
        "value": 64,
    }

    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "profile_fact",
                "evidence_ids": [next(iter(locator_ids))],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=payload,
    )
    assert result["valid"] is False
    assert "absent from observations" in result["violations"][0]["reason"]


def test_profile_fact_basis_does_not_require_a_case():
    payload = _payload()
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "profile_fact",
                "evidence_ids": [_evidence_id(payload, "genome_survey.heterozygosity")],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=payload,
    )

    assert result["valid"] is True


def test_retry_profile_fact_requires_structured_retry_failure_basis():
    payload = _with_profile(_payload(), {"assembly_review": {"busco_duplicate": 0.21}})
    payload["task"] = "retry_after_review_failure"
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "profile_fact",
                "evidence_ids": [_evidence_id(payload, "assembly_review.busco_duplicate")],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=payload,
    )

    assert result["valid"] is False
    assert "retry failure facts require basis=retry_failure" in result["violations"][0]["reason"]


def test_retry_profile_fact_allows_previous_attempt_parameter_value():
    payload = _with_profile(_payload(), {"previous_attempt": {"params": {"threads": 64}}})
    payload["task"] = "retry_after_execution_failure"
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "profile_fact",
                "evidence_ids": [_evidence_id(payload, "previous_attempt.params.threads")],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=payload,
    )

    assert result["valid"] is True


def test_retry_failure_basis_accepts_only_visible_signal_ids_or_paths():
    payload = _payload()
    payload["task"] = "retry_after_review_failure"
    payload["retry_evidence"] = {
        "signal_ids": ["review.busco_duplicate"],
        "signal_paths": ["assembly_review.busco_duplicate"],
    }
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "retry_failure",
                "retry_signals": ["review.invented"],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=payload,
    )

    assert result["valid"] is False
    assert "absent from structured prompt evidence" in result["violations"][0]["reason"]


def test_case_basis_requires_visible_case_and_span():
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "case",
                "case_ids": ["case_a"],
                "span_ids": ["methods.not_visible"],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=_payload(),
    )

    assert result["valid"] is False
    assert "absent from the prompt" in result["violations"][0]["reason"]


def test_l0_or_l1_output_is_still_rejected_by_catalog_validation():
    from app.schemas.parameter_catalog import MethodParameterCatalog, ParameterSpec
    from app.stages.assembly.parameter_validation import validate_recommended_parameters

    catalog = MethodParameterCatalog(
        method_id="hifiasm",
        tool="hifiasm",
        read_type="hifi",
        parameters={
            "output_prefix": ParameterSpec(cli=None, level=0, source="run_config", value_type="path"),
            "hg_size": ParameterSpec(cli=None, level=1, source="genome_survey", value_type="int"),
            "purge_level": ParameterSpec(
                cli=None,
                level=2,
                source="parameter_recommender",
                value_type="int",
            ),
        },
    )

    result = validate_recommended_parameters(
        method_id="hifiasm",
        catalog=catalog,
        recommended_params={"output_prefix": "/tmp/x", "hg_size": 1000, "purge_level": 1},
        readonly_facts={},
        rules={},
    )

    assert result["valid"] is False
    assert {violation["param"] for violation in result["violations"]} == {"output_prefix", "hg_size"}


def test_selected_case_ids_cannot_invent_non_shortlisted_case_without_parameter_evidence():
    result = validate_parameter_evidence(
        parameter_evidence=None,
        recommended_params={},
        prompt_payload=_payload(),
        declared_selected_case_ids=["invented_case"],
    )

    assert result["valid"] is False
    assert "non-shortlisted" in result["violations"][0]["reason"]


def test_hifiasm_recommended_parameter_requires_parameter_evidence():
    prompt_payload = _payload()
    prompt_payload["case_retrieval"] = prompt_payload.pop("case_selection")
    result = validate_parameter_evidence(
        parameter_evidence=None,
        recommended_params={"purge_level": 1},
        prompt_payload=prompt_payload,
        declared_selected_case_ids=[],
    )

    assert result["valid"] is False
    assert result["violations"][0]["param"] == "purge_level"
    assert "requires an evidence basis" in result["violations"][0]["reason"]


def test_each_cited_case_requires_its_own_visible_span():
    payload = _payload()
    payload["case_selection"]["selected_case_ids"] = ["case_a", "case_b"]
    payload["evidence_cases"].append(
        {
            "case_id": "case_b",
            "source_excerpts": [
                {"span_id": "methods.case_b", "document": "methods", "quote": "Case B rationale."}
            ],
        }
    )
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "case",
                "case_ids": ["case_a", "case_b"],
                "span_ids": ["methods.rationale"],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=payload,
        declared_selected_case_ids=["case_a", "case_b"],
    )

    assert result["valid"] is False
    assert "cases without a span" in result["violations"][0]["reason"]
    assert "case_b" in result["violations"][0]["reason"]


def _legacy_payload_without_spans() -> dict:
    """Flye-like prompt: selected cases present, no Methods span surface."""
    return _with_profile({
        "task": "recommend_initial",
        "case_selection": {
            "selected_case_ids": [
                "ont_vertebrate_moderate_coverage",
                "ont_large_plant_high_repeat",
            ],
            "selected_count": 2,
        },
        "selected_cases": [
            {
                "case_id": "ont_vertebrate_moderate_coverage",
                "source": "knowledge_pack",
                "params": {"asm_coverage": 50},
            },
            {
                "case_id": "ont_large_plant_high_repeat",
                "source": "knowledge_pack",
                "params": {"min_overlap": 5000},
            },
        ],
        "evidence_cases": [],
    }, {
        "assets": {"read_type": "ont"},
        "read_qc": {"total_bases": 1e10, "read_n50": 25000},
        "genome_survey": {"estimated_genome_size": 1e8, "repeat_content": 0.4},
        "sample": {"target_level": "draft"},
    })


def test_case_basis_without_span_surface_requires_only_case_ids():
    payload = _legacy_payload_without_spans()
    result = validate_parameter_evidence(
        parameter_evidence={
            "asm_coverage": {
                "bases": ["profile_fact", "skill_heuristic", "catalog_default", "case"],
                "case_ids": [
                    "ont_vertebrate_moderate_coverage",
                    "ont_large_plant_high_repeat",
                ],
                "span_ids": [],
                "evidence_ids": [
                    _evidence_id(payload, "assets.read_type"),
                    _evidence_id(payload, "read_qc.total_bases"),
                ],
                "heuristic_labels": ["high_coverage_downsample"],
            }
        },
        recommended_params={"asm_coverage": 100},
        prompt_payload=payload,
    )

    assert result["valid"] is True


def test_case_basis_without_span_surface_rejects_unknown_case_id():
    result = validate_parameter_evidence(
        parameter_evidence={
            "asm_coverage": {
                "basis": "case",
                "case_ids": ["not_in_prompt"],
                "span_ids": [],
            }
        },
        recommended_params={"asm_coverage": 100},
        prompt_payload=_legacy_payload_without_spans(),
    )

    assert result["valid"] is False
    assert "non-shortlisted" in result["violations"][0]["reason"]


def test_case_basis_without_span_surface_rejects_floating_span_ids():
    result = validate_parameter_evidence(
        parameter_evidence={
            "asm_coverage": {
                "basis": "case",
                "case_ids": ["ont_vertebrate_moderate_coverage"],
                "span_ids": ["methods.invented"],
            }
        },
        recommended_params={"asm_coverage": 100},
        prompt_payload=_legacy_payload_without_spans(),
    )

    assert result["valid"] is False
    assert "absent from the prompt" in result["violations"][0]["reason"]


def test_case_basis_with_span_surface_still_requires_span_ids():
    payload = _payload()
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "case",
                "case_ids": ["case_a"],
                "span_ids": [],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=payload,
    )

    assert result["valid"] is False
    assert "span_ids" in result["violations"][0]["reason"]
