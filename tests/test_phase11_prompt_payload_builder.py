"""Tests for prompt payload builder and knowledge pack integration (Phase 11).

NOTE: This test suite uses pytest fixtures and MUST be run with pytest:
    pytest tests/test_phase11_prompt_payload_builder.py -v
Running with unittest will give false-positive (0 tests).
"""

from __future__ import annotations

import json

import pytest

from app.knowledge.packs import load_knowledge_pack
from app.schemas.parameter_catalog import MethodParameterCatalog, ParameterSpec
from app.schemas.artifacts import file_sha256
from app.stages.assembly.prompt_payload import (
    ParameterPromptTask,
    build_prompt_payload,
    generate_payload_ref,
    generate_profile_ref,
)


# Test fixtures
@pytest.fixture
def sample_catalog() -> MethodParameterCatalog:
    """Create a sample parameter catalog with L0/L1/L2 parameters."""
    return MethodParameterCatalog(
        method_id="test_method",
        tool="test_tool",
        read_type="hifi",
        parameters={
            # L0 - should NOT appear in prompt or payload
            "output_dir": ParameterSpec(
                cli="--output",
                level=0,
                source="execution_context",
                value_type="path",
                fixed_value="/tmp/output",
            ),
            "input_file": ParameterSpec(
                cli="--input",
                level=0,
                source="assembly_out.input_ref",
                value_type="path",
            ),
            # L1 - should be in readonly_params only
            "read_type": ParameterSpec(
                cli=None,
                level=1,
                source="assembly_out.assembly_route",
                value_type="string",
                description="Read technology type",
            ),
            "genome_size_hint": ParameterSpec(
                cli=None,
                level=1,
                source="genome_survey.estimated_genome_size",
                value_type="int",
                description="Estimated genome size from survey",
            ),
            # L2 - should be in adjustable_params
            "threads": ParameterSpec(
                cli="--threads",
                level=2,
                source="llm_recommended",
                value_type="int",
                min=1,
                max=128,
                default=16,
                description="Number of threads",
            ),
            "kmer_size": ParameterSpec(
                cli="-k",
                level=2,
                source="llm_recommended",
                value_type="int",
                allowed_values=[17, 19, 21, 31, 41, 51],
                default=31,
                description="K-mer size for assembly graph",
            ),
            "purge_level": ParameterSpec(
                cli="--purge",
                level=2,
                source="llm_recommended",
                value_type="int",
                min=0,
                max=3,
                default=1,
                description="Purge duplication level",
            ),
        },
    )


@pytest.fixture
def sample_full_profile() -> dict:
    """Create a sample full profile."""
    return {
        "sample": {
            "sample_id": "test_sample_001",
            "organism": "Homo sapiens",
            "taxon_id": 9606,
            "clade": "vertebrate_mammalian",
            "ploidy": 2,
            "expected_chromosome_count": 23,
            "target_level": "chromosome",
        },
        "assets": {
            "read_type": "hifi",
            "read_format": "fastq",
            "has_hifi": True,
            "has_ont": False,
            "has_ultra_long": False,
            "has_hic": True,
            "has_trio": False,
            "has_rnaseq": True,
        },
        "read_qc": {
            "read_count": 5000000,
            "total_bases": 90000000000,
            "read_n50": 15000,
            "read_mean": 18000,
            "effective_coverage_1kb": 60,
            "mean_quality": 30,
        },
        "genome_survey": {
            "estimated_genome_size": 3100000000,
            "heterozygosity": 0.001,
            "repeat_content": 0.45,
            "kmer_peak_coverage": 55,
        },
        "previous_attempt": {},
        "assembly_review": {},
    }


class TestLevelSegregation:
    """Test that L0/L1/L2 parameters are properly segregated."""

    def test_l0_params_not_in_prompt_profile(self, sample_catalog, sample_full_profile):
        """L0 parameters should be excluded from prompt_profile."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        # L0 params should not appear in catalog_subset at all
        all_params_in_subset = {
            **payload["catalog_subset"]["readonly_params"],
            **payload["catalog_subset"]["adjustable_params"],
        }
        assert "output_dir" not in all_params_in_subset
        assert "input_file" not in all_params_in_subset

        # L0 should not be in readonly or adjustable
        assert "output_dir" not in payload["readonly_params"]
        assert "output_dir" not in payload["adjustable_params"]
        assert "input_file" not in payload["readonly_params"]
        assert "input_file" not in payload["adjustable_params"]

    def test_l1_params_readonly_in_payload(self, sample_catalog, sample_full_profile):
        """L1 parameters should be in readonly_params, not adjustable."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        # L1 params in readonly
        assert "read_type" in payload["readonly_params"]
        assert "genome_size_hint" in payload["readonly_params"]
        assert payload["readonly_params"]["read_type"]["level"] == 1
        assert payload["readonly_params"]["genome_size_hint"]["level"] == 1

        # L1 params NOT in adjustable
        assert "read_type" not in payload["adjustable_params"]
        assert "genome_size_hint" not in payload["adjustable_params"]

    def test_l2_params_adjustable_in_payload(self, sample_catalog, sample_full_profile):
        """L2 parameters should be in adjustable_params, not readonly."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        # L2 params in adjustable
        assert "threads" in payload["adjustable_params"]
        assert "kmer_size" in payload["adjustable_params"]
        assert "purge_level" in payload["adjustable_params"]
        assert payload["adjustable_params"]["threads"]["level"] == 2
        assert payload["adjustable_params"]["kmer_size"]["level"] == 2

        # L2 params NOT in readonly
        assert "threads" not in payload["readonly_params"]
        assert "kmer_size" not in payload["readonly_params"]

    def test_catalog_subset_only_l1_l2(self, sample_catalog, sample_full_profile):
        """catalog_subset should only contain L1 and L2 parameters."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        all_subset_params = {
            **payload["catalog_subset"]["readonly_params"],
            **payload["catalog_subset"]["adjustable_params"],
        }

        # Check all params in subset are L1 or L2
        for key, spec in all_subset_params.items():
            assert spec["level"] in [1, 2], f"Parameter {key} has level {spec['level']}, expected 1 or 2"

        # Verify counts
        assert len(payload["catalog_subset"]["readonly_params"]) == 2  # read_type, genome_size_hint
        assert len(payload["catalog_subset"]["adjustable_params"]) == 3  # threads, kmer_size, purge_level


class TestTaskScopedSlicing:
    """Test that prompt_profile is correctly sliced by task type."""

    def test_recommend_initial_profile_fields(self, sample_catalog, sample_full_profile):
        """recommend_initial should include sample, assets, read_qc, genome_survey."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        profile = payload["prompt_profile"]
        assert "sample" in profile
        assert "assets" in profile
        assert "read_qc" in profile
        assert "genome_survey" in profile
        assert "previous_attempt" not in profile
        assert "assembly_review" not in profile

        # Sample should be filtered to initial fields
        assert "sample_id" in profile["sample"]
        assert "organism" in profile["sample"]
        assert "ploidy" in profile["sample"]

    def test_retry_after_execution_failure_profile(self, sample_catalog, sample_full_profile):
        """retry_after_execution_failure should include previous_attempt execution fields."""
        full_profile_with_attempt = {
            **sample_full_profile,
            "previous_attempt": {
                "attempt_id": "attempt_001",
                "method_id": "hifiasm",
                "exit_code": 137,
                "failure_class": "memory_exhausted",
                "stderr_tail": "oom-kill",
                "params": {"threads": 64},
            },
        }

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=full_profile_with_attempt,
            task="retry_after_execution_failure",
        )

        profile = payload["prompt_profile"]
        assert "sample" in profile
        assert "assets" in profile
        assert "previous_attempt" in profile
        assert "read_qc" not in profile
        assert "genome_survey" not in profile

        # Previous attempt should have execution fields
        attempt = profile["previous_attempt"]
        assert "attempt_id" in attempt
        assert "exit_code" in attempt
        assert "failure_class" in attempt

    def test_retry_after_review_failure_profile(self, sample_catalog, sample_full_profile):
        """retry_after_review_failure should include assembly_review metrics."""
        full_profile_with_review = {
            **sample_full_profile,
            "previous_attempt": {
                "attempt_id": "attempt_002",
                "method_id": "hifiasm",
                "params": {"kmer_size": 51},
            },
            "assembly_review": {
                "assembly_size": 3000000000,
                "contig_count": 500,
                "contig_n50": 1000000,
                "busco_complete": 92.5,
            },
        }

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=full_profile_with_review,
            task="retry_after_review_failure",
        )

        profile = payload["prompt_profile"]
        assert "sample" in profile
        assert "previous_attempt" in profile
        assert "assembly_review" in profile
        assert "assets" not in profile
        assert "read_qc" not in profile

        # Assembly review should have metrics
        review = profile["assembly_review"]
        assert "contig_count" in review
        assert "contig_n50" in review
        assert "busco_complete" in review


class TestKnowledgePackLoading:
    """Test knowledge pack loading from filesystem."""

    def test_load_rules_yaml(self):
        """rules.yaml should load and contain rules list."""
        pack = load_knowledge_pack("parameter_recommender")
        assert "rules" in pack
        assert isinstance(pack["rules"], dict)
        assert "rules" in pack["rules"]
        assert isinstance(pack["rules"]["rules"], list)
        assert len(pack["rules"]["rules"]) > 0

        # Check first rule structure
        rule = pack["rules"]["rules"][0]
        assert "rule_id" in rule
        assert "condition" in rule
        assert "constraints" in rule
        assert "reason" in rule

    def test_load_cases_yaml(self):
        """cases.yaml should load with all required fields."""
        pack = load_knowledge_pack("parameter_recommender")
        assert "cases" in pack
        assert isinstance(pack["cases"], list)
        assert len(pack["cases"]) > 0

        # Check first case structure: cases are evidence observations, not prompt answers.
        case = pack["cases"][0]
        assert "case_id" in case
        assert "source" in case
        assert "features" in case
        assert "params" in case
        assert "quality" in case
        assert "evidence_note" in case

    def test_load_skill_md(self):
        """SKILL.md should load as string."""
        pack = load_knowledge_pack("parameter_recommender")
        assert "skill" in pack
        assert isinstance(pack["skill"], str)
        assert len(pack["skill"]) > 100  # Should be substantial content
        assert "L0" in pack["skill"]
        assert "L1" in pack["skill"]
        assert "L2" in pack["skill"]

    def test_load_nonexistent_pack_raises(self):
        """Loading nonexistent pack should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_knowledge_pack("nonexistent_pack_12345")


class TestPayloadStructure:
    """Test that payload contains all required keys and structure."""

    def test_payload_contains_all_required_keys(self, sample_catalog, sample_full_profile):
        """Payload should have all documented keys."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        required_keys = {
            "method_id",
            "task",
            "input_profile_ref",
            "prompt_payload_ref",
            "input_profile_hash",
            "prompt_payload_hash",
            "prompt_profile",
            "catalog_subset",
            "rules",
            "selected_cases",
            "case_selection",
            "case_retrieval",
            "evidence_cases",
            "recommendation_policy",
            "skill_instructions",
            "readonly_params",
            "adjustable_params",
        }

        assert set(payload.keys()) == required_keys

    def test_readonly_params_are_l1_only(self, sample_catalog, sample_full_profile):
        """readonly_params should only contain L1 parameters."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        for key, spec in payload["readonly_params"].items():
            assert spec["level"] == 1, f"readonly_params[{key}] has level {spec['level']}, expected 1"

    def test_adjustable_params_are_l2_only(self, sample_catalog, sample_full_profile):
        """adjustable_params should only contain L2 parameters."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        for key, spec in payload["adjustable_params"].items():
            assert spec["level"] == 2, f"adjustable_params[{key}] has level {spec['level']}, expected 2"

    def test_catalog_subset_structure(self, sample_catalog, sample_full_profile):
        """catalog_subset should have readonly_params and adjustable_params."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        assert "readonly_params" in payload["catalog_subset"]
        assert "adjustable_params" in payload["catalog_subset"]
        assert isinstance(payload["catalog_subset"]["readonly_params"], dict)
        assert isinstance(payload["catalog_subset"]["adjustable_params"], dict)


class TestReferenceGeneration:
    """Test reference ID generation for replay and audit."""

    def test_input_profile_ref_stable(self, sample_catalog, sample_full_profile):
        """Same profile should generate same ref."""
        payload1 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )
        payload2 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        assert payload1["input_profile_ref"] == payload2["input_profile_ref"]
        assert len(payload1["input_profile_ref"]) == 64  # SHA256 hex

    def test_input_profile_ref_changes_on_mutation(self, sample_catalog, sample_full_profile):
        """Modified profile should generate different ref."""
        payload1 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        modified_profile = {**sample_full_profile}
        modified_profile["sample"] = {**sample_full_profile["sample"], "organism": "Mus musculus"}

        payload2 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=modified_profile,
            task="recommend_initial",
        )

        assert payload1["input_profile_ref"] != payload2["input_profile_ref"]

    def test_prompt_payload_ref_stable(self, sample_catalog, sample_full_profile):
        """Same inputs should generate same payload ref."""
        payload1 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )
        payload2 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        assert payload1["prompt_payload_ref"] == payload2["prompt_payload_ref"]
        assert len(payload1["prompt_payload_ref"]) == 64  # SHA256 hex

    def test_refs_are_distinct(self, sample_catalog, sample_full_profile):
        """input_profile_ref and prompt_payload_ref should be different."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        assert payload["input_profile_ref"] != payload["prompt_payload_ref"]

    def test_generate_profile_ref_directly(self):
        """Test generate_profile_ref function directly."""
        profile1 = {"key": "value", "nested": {"a": 1}}
        profile2 = {"key": "value", "nested": {"a": 1}}
        profile3 = {"key": "different", "nested": {"a": 1}}

        ref1 = generate_profile_ref(profile1)
        ref2 = generate_profile_ref(profile2)
        ref3 = generate_profile_ref(profile3)

        assert ref1 == ref2  # Same content
        assert ref1 != ref3  # Different content
        assert len(ref1) == 64

    def test_generate_payload_ref_directly(self):
        """Test generate_payload_ref function directly."""
        payload1 = {"method": "test", "params": [1, 2, 3]}
        payload2 = {"method": "test", "params": [1, 2, 3]}
        payload3 = {"method": "test", "params": [1, 2, 4]}

        ref1 = generate_payload_ref(payload1)
        ref2 = generate_payload_ref(payload2)
        ref3 = generate_payload_ref(payload3)

        assert ref1 == ref2  # Same content
        assert ref1 != ref3  # Different content
        assert len(ref1) == 64


class TestReplayCapability:
    """Test that full profile can be replayed from references."""

    def test_full_profile_replay(self, sample_catalog, sample_full_profile):
        """Same full_profile should produce same input_profile_ref for replay."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        # In real system, we'd look up full_profile by input_profile_ref
        # Here we verify the ref is stable for the same input
        ref = payload["input_profile_ref"]
        computed_ref = generate_profile_ref(sample_full_profile)

        assert ref == computed_ref

    def test_payload_includes_full_audit_trail(self, sample_catalog, sample_full_profile):
        """Payload should include all components for full audit trail."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        # Verify all audit trail components present
        assert "input_profile_ref" in payload
        assert "prompt_payload_ref" in payload
        assert "method_id" in payload
        assert "task" in payload
        assert "prompt_profile" in payload
        assert "catalog_subset" in payload
        assert "rules" in payload
        assert "selected_cases" in payload
        assert "case_selection" in payload
        assert "skill_instructions" in payload

        # Verify we can reconstruct what was sent to LLM
        llm_input_components = {
            "profile": payload["prompt_profile"],
            "catalog": payload["catalog_subset"],
            "rules": payload["rules"],
            "cases": payload["selected_cases"],
            "instructions": payload["skill_instructions"],
        }
        assert all(v is not None for v in llm_input_components.values())


class TestArtifactWriter:
    """Test artifact writing functionality with session/attempt context."""

    def test_write_artifacts_with_session_id(self, sample_catalog, sample_full_profile, tmp_path):
        """Write artifacts with session_id should create session-scoped paths."""
        session_id = "test_session_001"
        attempt_id = 1

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id=session_id,
            attempt_id=attempt_id,
            data_dir=tmp_path,
        )

        # Verify profile artifact was written to session-scoped path
        expected_profile_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "input_profile.json"
        assert expected_profile_path.exists(), f"Profile artifact not found at {expected_profile_path}"

        # Verify payload artifact was written to attempt-scoped path
        expected_payload_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "prompt_payload.json"
        assert expected_payload_path.exists(), f"Payload artifact not found at {expected_payload_path}"

        # Verify returned refs are path-based (not hash-based) when session_id is provided
        assert payload["input_profile_ref"].startswith("data/artifacts/assembly/")
        assert payload["input_profile_ref"] == f"data/artifacts/assembly/{session_id}/parameter_attempt_{attempt_id}/input_profile.json"
        assert payload["prompt_payload_ref"].startswith("data/artifacts/assembly/")
        assert payload["prompt_payload_ref"] == f"data/artifacts/assembly/{session_id}/parameter_attempt_{attempt_id}/prompt_payload.json"

    def test_write_artifacts_without_session_id(self, sample_catalog, sample_full_profile, tmp_path):
        """Artifact writes require final session/attempt refs."""
        with pytest.raises(ValueError):
            build_prompt_payload(
                method_id="test_method",
                catalog=sample_catalog,
                full_profile=sample_full_profile,
                task="recommend_initial",
                write_artifacts=True,
                session_id=None,
                attempt_id=None,
                data_dir=tmp_path,
            )

    def test_artifact_content_matches_input(self, sample_catalog, sample_full_profile, tmp_path):
        """Written artifacts should contain correct content."""
        session_id = "test_session_002"
        attempt_id = 2

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id=session_id,
            attempt_id=attempt_id,
            data_dir=tmp_path,
        )

        # Read and verify profile artifact
        profile_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "input_profile.json"
        with profile_path.open("r") as f:
            loaded_profile = json.load(f)
        assert loaded_profile == sample_full_profile

        # Read and verify payload artifact
        payload_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "prompt_payload.json"
        with payload_path.open("r") as f:
            loaded_payload = json.load(f)

        # Verify key fields are preserved
        assert loaded_payload["method_id"] == "test_method"
        assert loaded_payload["task"] == "recommend_initial"
        assert loaded_payload["prompt_profile"] == payload["prompt_profile"]
        assert loaded_payload["catalog_subset"] == payload["catalog_subset"]

    def test_load_roundtrip_with_path_refs(self, sample_catalog, sample_full_profile, tmp_path):
        """Test that load roundtrip with path-based refs works (P1 fix implemented)."""
        from app.stages.assembly.prompt_payload import load_profile_by_ref, load_payload_by_ref

        # Write artifacts with session_id (path-based refs)
        session_id = "test_session_003"
        attempt_id = 3

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id=session_id,
            attempt_id=attempt_id,
            data_dir=tmp_path,
        )

        # Verify files exist at correct paths
        profile_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "input_profile.json"
        assert profile_path.exists()

        payload_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "prompt_payload.json"
        assert payload_path.exists()

        # Load using path refs (should work with the fix)
        loaded_profile = load_profile_by_ref(payload["input_profile_ref"], data_dir=tmp_path)
        assert loaded_profile == sample_full_profile

        loaded_payload = load_payload_by_ref(payload["prompt_payload_ref"], data_dir=tmp_path)
        assert loaded_payload["method_id"] == payload["method_id"]
        assert loaded_payload["task"] == payload["task"]
        assert loaded_payload["prompt_profile"] == payload["prompt_profile"]
        assert loaded_payload["prompt_payload_ref"] == payload["prompt_payload_ref"]
        assert loaded_payload["prompt_payload_hash"] == generate_payload_ref(
            {k: v for k, v in payload.items() if k not in ("prompt_payload_ref", "prompt_payload_hash")}
        )
        assert loaded_payload["prompt_payload_hash"] != file_sha256(payload_path)

    def test_multiple_attempts_same_session(self, sample_catalog, sample_full_profile, tmp_path):
        """Multiple attempts in same session should create separate payload files."""
        session_id = "test_session_004"

        # Write attempt 1
        payload1 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id=session_id,
            attempt_id=1,
            data_dir=tmp_path,
        )

        # Write attempt 2 with modified profile
        modified_profile = {**sample_full_profile}
        modified_profile["sample"] = {**sample_full_profile["sample"], "organism": "Mus musculus"}

        payload2 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=modified_profile,
            task="retry_after_execution_failure",
            write_artifacts=True,
            session_id=session_id,
            attempt_id=2,
            data_dir=tmp_path,
        )

        # Verify both payload files exist
        payload1_path = tmp_path / "artifacts" / "assembly" / session_id / "parameter_attempt_1" / "prompt_payload.json"
        payload2_path = tmp_path / "artifacts" / "assembly" / session_id / "parameter_attempt_2" / "prompt_payload.json"

        assert payload1_path.exists()
        assert payload2_path.exists()

        # Verify refs are different
        assert payload1["prompt_payload_ref"] != payload2["prompt_payload_ref"]
        assert payload1["input_profile_ref"] != payload2["input_profile_ref"]
        assert "parameter_attempt_1/input_profile.json" in payload1["input_profile_ref"]
        assert "parameter_attempt_2/input_profile.json" in payload2["input_profile_ref"]

        # Verify content is different
        with payload1_path.open("r") as f:
            content1 = json.load(f)
        with payload2_path.open("r") as f:
            content2 = json.load(f)

        assert content1["task"] == "recommend_initial"
        assert content2["task"] == "retry_after_execution_failure"
        assert content1["prompt_profile"]["sample"]["organism"] == "Homo sapiens"
        assert content2["prompt_profile"]["sample"]["organism"] == "Mus musculus"

    def test_write_artifacts_false_no_files_created(self, sample_catalog, sample_full_profile, tmp_path):
        """With write_artifacts=False, no files should be created."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=False,
            session_id="test_session_005",
            attempt_id=1,
            data_dir=tmp_path,
        )

        # Verify no artifacts directory was created
        artifacts_dir = tmp_path / "artifacts"
        assert not artifacts_dir.exists() or len(list(artifacts_dir.rglob("*.json"))) == 0

        # Verify refs are hash-based (not path-based) even with session_id provided
        assert len(payload["input_profile_ref"]) == 64
        assert len(payload["prompt_payload_ref"]) == 64


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_profile_sections(self, sample_catalog):
        """Empty profile sections should be handled gracefully."""
        minimal_profile = {
            "sample": {"sample_id": "test"},
            "assets": {},
            "read_qc": {},
            "genome_survey": {},
            "previous_attempt": {},
            "assembly_review": {},
        }

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=minimal_profile,
            task="recommend_initial",
        )

        assert payload["input_profile_ref"] is not None
        assert payload["prompt_payload_ref"] is not None

    def test_catalog_with_only_l0(self):
        """Catalog with only L0 parameters should produce empty readonly/adjustable."""
        catalog_l0_only = MethodParameterCatalog(
            method_id="l0_only",
            tool="test",
            read_type="hifi",
            parameters={
                "output": ParameterSpec(cli="--out", level=0, source="ctx", value_type="path"),
            },
        )
        minimal_profile = {
            "sample": {},
            "assets": {},
            "read_qc": {},
            "genome_survey": {},
            "previous_attempt": {},
            "assembly_review": {},
        }

        payload = build_prompt_payload(
            method_id="l0_only",
            catalog=catalog_l0_only,
            full_profile=minimal_profile,
            task="recommend_initial",
        )

        assert len(payload["readonly_params"]) == 0
        assert len(payload["adjustable_params"]) == 0

    def test_catalog_with_only_l2(self):
        """Catalog with only L2 parameters should produce empty readonly."""
        catalog_l2_only = MethodParameterCatalog(
            method_id="l2_only",
            tool="test",
            read_type="hifi",
            parameters={
                "threads": ParameterSpec(cli="--threads", level=2, source="llm", value_type="int"),
            },
        )
        minimal_profile = {
            "sample": {},
            "assets": {},
            "read_qc": {},
            "genome_survey": {},
            "previous_attempt": {},
            "assembly_review": {},
        }

        payload = build_prompt_payload(
            method_id="l2_only",
            catalog=catalog_l2_only,
            full_profile=minimal_profile,
            task="recommend_initial",
        )

        assert len(payload["readonly_params"]) == 0
        assert len(payload["adjustable_params"]) == 1

    def test_payload_uses_selected_cases_not_full_database(self, sample_catalog, sample_full_profile):
        """Prompt payload should include a selected evidence subset, not every stored case."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        pack = load_knowledge_pack("parameter_recommender")
        assert "cases" not in payload
        assert len(payload["selected_cases"]) <= 8
        assert len(payload["selected_cases"]) < len(pack["cases"])
        assert payload["case_selection"]["selected_case_ids"] == [
            case["case_id"] for case in payload["selected_cases"]
        ]

    def test_cyclone_route_selects_ont_evidence_cases(self, sample_catalog, sample_full_profile):
        cyclone_profile = {
            **sample_full_profile,
            "assets": {
                **sample_full_profile["assets"],
                "assembly_route": "cyclone",
                "read_type": "ont",
                "has_hifi": False,
                "has_ont": True,
            },
        }

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=cyclone_profile,
            task="recommend_initial",
        )

        assert payload["prompt_profile"]["assets"]["assembly_route"] == "cyclone"
        assert payload["prompt_profile"]["assets"]["read_type"] == "ont"
        assert payload["selected_cases"]
        assert {case["features"]["read_type"] for case in payload["selected_cases"]} == {"ont"}


class TestRealCatalogCompatibility:
    """Test that knowledge pack examples reference real parameters from catalogs."""

    @pytest.mark.parametrize(
        "method_id,catalog_id",
        [
            ("assembly.candidate.flye", "flye"),
            ("assembly.candidate.nextdenovo", "nextdenovo"),
            ("assembly.candidate.hifiasm", "hifiasm"),
            ("assembly.candidate.hifiasm_ont", "hifiasm_ont"),
        ],
    )
    def test_executable_assembly_methods_have_phase11_catalogs(self, method_id, catalog_id):
        """All executable assembly methods should use the typed parameter-attempt catalog contract."""
        from app.stages.assembly.parameter_catalog import load_parameter_catalog

        catalog = load_parameter_catalog(method_id)

        assert catalog.method_id == catalog_id
        assert sum(1 for p in catalog.parameters.values() if p.level == 0) > 0
        assert sum(1 for p in catalog.parameters.values() if p.level == 1) > 0
        assert sum(1 for p in catalog.parameters.values() if p.level == 2) > 0

    @pytest.mark.parametrize("method_id", ["hifiasm", "hifiasm_ont"])
    def test_cases_yaml_params_are_l2_in_real_catalog(self, method_id):
        """All observed parameter values in cases.yaml must exist in catalog as L2."""
        from app.stages.assembly.parameter_catalog import load_parameter_catalog

        catalog = load_parameter_catalog(method_id)
        pack = load_knowledge_pack("parameter_recommender")

        # Extract all parameter keys from observed params across cases.
        all_param_keys = set()
        for case in pack["cases"]:
            params = case.get("params") or {}
            values = params.get("values") or {}
            all_param_keys.update(values.keys())

        # Verify each parameter exists in catalog and is L2
        for param_key in all_param_keys:
            assert param_key in catalog.parameters, f"Parameter {param_key!r} from cases.yaml not found in {method_id} catalog"
            param_spec = catalog.parameters[param_key]
            assert param_spec.level == 2, f"Parameter {param_key!r} from cases.yaml is L{param_spec.level}, expected L2 in {method_id}"

    @pytest.mark.parametrize("method_id", ["hifiasm", "hifiasm_ont"])
    def test_rules_yaml_params_are_l2_in_real_catalog(self, method_id):
        """All constraint keys in rules.yaml must exist in catalog as L2."""
        from app.stages.assembly.parameter_catalog import load_parameter_catalog

        catalog = load_parameter_catalog(method_id)
        pack = load_knowledge_pack("parameter_recommender")

        # Extract all constraint parameter keys from rules
        all_constraint_keys = set()
        rules_list = pack["rules"].get("rules", []) if isinstance(pack["rules"], dict) else pack["rules"]
        for rule in rules_list:
            if "constraints" in rule:
                all_constraint_keys.update(rule["constraints"].keys())

        # Verify each parameter exists in catalog and is L2
        for param_key in all_constraint_keys:
            assert param_key in catalog.parameters, f"Parameter {param_key!r} from rules.yaml not found in {method_id} catalog"
            param_spec = catalog.parameters[param_key]
            assert param_spec.level == 2, f"Parameter {param_key!r} from rules.yaml is L{param_spec.level}, expected L2 in {method_id}"

    @pytest.mark.parametrize("method_id", ["hifiasm", "hifiasm_ont"])
    def test_full_payload_with_real_catalog(self, method_id):
        """Verify real catalog has expected L1/L2 structure for payload building."""
        from app.stages.assembly.parameter_catalog import load_parameter_catalog

        catalog = load_parameter_catalog(method_id)

        # Count L0, L1, L2 parameters
        l0_count = sum(1 for p in catalog.parameters.values() if p.level == 0)
        l1_count = sum(1 for p in catalog.parameters.values() if p.level == 1)
        l2_count = sum(1 for p in catalog.parameters.values() if p.level == 2)

        # Verify we have parameters at each level
        assert l0_count > 0, f"{method_id} catalog should have L0 parameters"
        assert l1_count > 0, f"{method_id} catalog should have L1 parameters for readonly_params"
        assert l2_count > 0, f"{method_id} catalog should have L2 parameters for adjustable_params"

        # Verify specific L1 parameters that are referenced in profile building
        assert "hg_size" in catalog.parameters, f"{method_id} catalog missing hg_size"
        assert catalog.parameters["hg_size"].level == 1, f"{method_id} hg_size should be L1"
        assert "hom_cov" in catalog.parameters, f"{method_id} catalog missing hom_cov"
        assert catalog.parameters["hom_cov"].level == 1, f"{method_id} hom_cov should be L1"
        assert "n_hap" in catalog.parameters, f"{method_id} catalog missing n_hap"
        assert catalog.parameters["n_hap"].level == 1, f"{method_id} n_hap should be L1"

        # Verify all L2 parameters have required fields for LLM recommendation
        for key, spec in catalog.parameters.items():
            if spec.level == 2:
                assert spec.source == "parameter_recommender", f"{method_id} L2 param {key} should have source='parameter_recommender'"
                assert spec.value_type is not None, f"{method_id} L2 param {key} should have value_type"
                # CLI can be None for some parameters, but most should have it
                # description can be None but most should have it

        # Verify no parameters have level > 2
        for key, spec in catalog.parameters.items():
            assert spec.level in [0, 1, 2], f"{method_id} parameter {key} has invalid level {spec.level}"

    @pytest.mark.parametrize("method_id,read_type", [("hifiasm", "hifi"), ("hifiasm_ont", "ont")])
    def test_selected_cases_match_read_type(self, method_id, read_type):
        """Verify selected cases match the read_type and no cross-contamination occurs."""
        from app.stages.assembly.parameter_catalog import load_parameter_catalog

        # Load real catalog
        catalog = load_parameter_catalog(method_id)

        # Build sample profile with the specified read_type
        sample_profile = {
            "sample": {
                "sample_id": "test_sample_001",
                "organism": "Homo sapiens",
                "taxon_id": 9606,
                "clade": "vertebrate_mammalian",
                "ploidy": 2,
                "expected_chromosome_count": 23,
                "target_level": "chromosome",
            },
            "assets": {
                "read_type": read_type,
                "read_format": "fastq",
                "has_hifi": read_type == "hifi",
                "has_ont": read_type == "ont",
                "has_ultra_long": False,
                "has_hic": True,
                "has_trio": False,
                "has_rnaseq": True,
            },
            "read_qc": {
                "read_count": 5000000,
                "total_bases": 90000000000,
                "read_n50": 15000,
                "read_mean": 18000,
                "effective_coverage_1kb": 60,
                "mean_quality": 30,
            },
            "genome_survey": {
                "estimated_genome_size": 3100000000,
                "heterozygosity": 0.001,
                "repeat_content": 0.45,
                "kmer_peak_coverage": 55,
            },
            "previous_attempt": {},
            "assembly_review": {},
        }

        # Build prompt payload
        payload = build_prompt_payload(
            method_id=method_id,
            catalog=catalog,
            full_profile=sample_profile,
            task="recommend_initial",
        )

        # Extract selected_cases from payload
        selected_cases = payload["selected_cases"]

        # Assert: ALL selected_cases have matching read_type
        for case in selected_cases:
            case_read_type = case.get("features", {}).get("read_type")
            assert case_read_type == read_type, (
                f"Case {case['case_id']} has read_type={case_read_type!r}, "
                f"expected {read_type!r} for method {method_id}"
            )

        # Assert: NO cross-contamination (verify opposite read_type is not present)
        opposite_read_type = "ont" if read_type == "hifi" else "hifi"
        for case in selected_cases:
            case_read_type = case.get("features", {}).get("read_type")
            assert case_read_type != opposite_read_type, (
                f"Cross-contamination detected: Case {case['case_id']} has read_type={case_read_type!r}, "
                f"but expected no {opposite_read_type!r} cases for method {method_id}"
            )

        # Verify params.tool compatibility (cases should be for the correct method)
        # Note: cases use params.tool to denote method_id, not the executable tool
        for case in selected_cases:
            case_method = case.get("params", {}).get("tool")
            assert case_method == method_id, (
                f"Case {case['case_id']} has params.tool={case_method!r}, "
                f"expected {method_id!r} for method {method_id}"
            )


def test_hifiasm_writes_ref_backed_methods_evidence_payload(
    sample_catalog, sample_full_profile, tmp_path, monkeypatch
):
    evidence_case = {
        "case_id": "case_a",
        "applicability": {"read_type": "hifi"},
        "source": {"kind": "paper", "title": "Example"},
        "numeric_match": {"distance": 0.1},
        "observed_facts": {"coverage": {"value": 30}},
        "intervention": {"tool": "hifiasm", "parameters": {}},
        "rationale_claims": [],
        "source_excerpts": [
            {"span_id": "methods.a", "document": "methods", "quote": "Exact excerpt."}
        ],
        "outcomes": {},
    }
    selection = {
        "retrieval_backend": "hifiasm_numeric",
        "selected_case_ids": ["case_a"],
        "candidate_case_ids": ["case_a"],
        "eligible_case_count": 1,
        "warning": None,
        "query": {"query_id": "s1:parameter_attempt_0", "method_id": "hifiasm"},
        "trace": [{"case_id": "case_a", "decision": "candidate"}],
    }
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.load_knowledge_pack",
        lambda *_args, **_kwargs: {
            "cases": [],
            "rules": {"rules": []},
            "skill": "Use conservative evidence.",
            "strategies": {},
            "pack_root": tmp_path,
        },
    )
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.select_evidence_cases",
        lambda *_args, **_kwargs: ([evidence_case], selection),
    )

    payload = build_prompt_payload(
        method_id="hifiasm",
        catalog=sample_catalog.model_copy(update={"method_id": "hifiasm"}),
        full_profile=sample_full_profile,
        task="recommend_initial",
        write_artifacts=True,
        session_id="s1",
        attempt_id=0,
        data_dir=tmp_path,
    )

    evidence_path = tmp_path / payload["evidence_payload_ref"].removeprefix("data/")
    assert evidence_path.name == "evidence_payload.json"
    assert evidence_path.exists()
    assert payload["case_retrieval"]["evidence_payload_ref"] == payload["evidence_payload_ref"]
    assert payload["methods_evidence"]["response_contract"] == "case_assessments_and_recommendation"
    loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert loaded["candidate_case_ids"] == ["case_a"]
    assert loaded["cases"][0]["source_excerpts"][0]["quote"] == "Exact excerpt."


def test_hifiasm_limits_final_methods_evidence_to_eight_cases(
    sample_catalog, sample_full_profile, tmp_path, monkeypatch
):
    evidence_cases = [
        {
            "case_id": f"case_{index:02d}",
            "applicability": {"read_type": "hifi"},
            "source": {"kind": "paper", "title": f"Example {index}"},
            "numeric_match": {"distance": index / 100},
            "observed_facts": {"coverage": {"value": 30 + index}},
            "intervention": {"tool": "hifiasm", "parameters": {}},
            "rationale_claims": [],
            "source_excerpts": [
                {
                    "span_id": f"methods.{index}",
                    "document": "methods",
                    "quote": f"Exact excerpt {index}.",
                }
            ],
            "outcomes": {},
        }
        for index in range(12)
    ]
    candidate_ids = [case["case_id"] for case in evidence_cases]
    selection = {
        "retrieval_backend": "hifiasm_numeric",
        "selected_case_ids": candidate_ids,
        "candidate_case_ids": candidate_ids,
        "eligible_case_count": 12,
        "warning": None,
        "query": {"query_id": "s1:parameter_attempt_0", "method_id": "hifiasm"},
        "trace": [
            {"case_id": case_id, "decision": "candidate"}
            for case_id in candidate_ids
        ],
    }
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.load_knowledge_pack",
        lambda *_args, **_kwargs: {
            "cases": [],
            "rules": {"rules": []},
            "skill": "Use conservative evidence.",
            "strategies": {},
            "pack_root": tmp_path,
        },
    )
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.select_evidence_cases",
        lambda *_args, **_kwargs: (evidence_cases, selection),
    )

    payload = build_prompt_payload(
        method_id="hifiasm",
        catalog=sample_catalog.model_copy(update={"method_id": "hifiasm"}),
        full_profile=sample_full_profile,
        task="recommend_initial",
        write_artifacts=True,
        session_id="s1",
        attempt_id=0,
        data_dir=tmp_path,
    )

    assert payload["case_retrieval"]["candidate_case_ids"] == candidate_ids
    assert payload["case_retrieval"]["evidence_case_ids"] == candidate_ids[:8]
    assert [case["case_id"] for case in payload["evidence_cases"]] == candidate_ids[:8]
    evidence_path = tmp_path / payload["evidence_payload_ref"].removeprefix("data/")
    loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert loaded["candidate_case_ids"] == candidate_ids[:8]
    assert loaded["retrieval_candidate_case_ids"] == candidate_ids
