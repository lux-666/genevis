"""Shared catalog-value validation at the parameter resolver boundary."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from app.domains.parameter_recommendation.resolution import resolve_parameters
from app.schemas.artifacts import ArtifactRef
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    AutoHiCRecommendationContext,
    CanonicalGenomeFacts,
    CanonicalRecommendationFacts,
    QuantityFact,
    QuantityOrigin,
    RecommendationDependencyRefs,
    RecommendationInputSnapshotV3,
    T2TRecommendationContext,
)
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.stages.hic_scaffolding.parameters.catalog import (
    load_autohic_parameter_catalog,
)
from app.stages.t2t.parameters.catalog import load_t2t_parameter_catalog


def _ref(name: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"artifact:{name}",
        path_ref=f"artifacts/test/{name}.json",
        sha256=sha256(name.encode("utf-8")).hexdigest(),
        size_bytes=1,
        artifact_type="test.artifact",
    )


def _assembly_snapshot(
    method_id: str = "assembly.test.parameter_validation",
) -> RecommendationInputSnapshotV3:
    return RecommendationInputSnapshotV3(
        schema_version=3,
        method_id=method_id,
        task="recommend_initial",
        facts=CanonicalRecommendationFacts(
            genome=CanonicalGenomeFacts(
                effective_genome_size=QuantityFact(
                    value=2_500_000,
                    unit="bp",
                    origin=QuantityOrigin(kind="provided", producer="run_config"),
                )
            )
        ),
        stage_context=AssemblyRecommendationContext(
            stage="assembly",
            assembly_route="cyclone",
            read_type="ont_hq",
            target_level="draft",
        ),
    )


def _autohic_snapshot() -> RecommendationInputSnapshotV3:
    return RecommendationInputSnapshotV3(
        schema_version=3,
        method_id="hic_scaffolding.candidate.autohic",
        task="recommend_initial",
        facts=CanonicalRecommendationFacts(),
        stage_context=AutoHiCRecommendationContext(
            stage="hic_scaffolding",
            library_protocols=(),
        ),
    )


def _t2t_snapshot() -> RecommendationInputSnapshotV3:
    return RecommendationInputSnapshotV3(
        schema_version=3,
        method_id="t2t.alignment.minimap2",
        task="recommend_initial",
        facts=CanonicalRecommendationFacts(),
        stage_context=T2TRecommendationContext(
            stage="t2t",
            resolved_telomere_motif="TTAGGG",
            motif_source="provided_species_fact",
            long_read_asset_count=1,
        ),
        dependency_refs=RecommendationDependencyRefs(
            stage_input_refs=(_ref("assembly"), _ref("long-read")),
            motif_resolution_ref=_ref("motif"),
        ),
    )


def _l2_catalog(
    value_type: str,
    *,
    allowed_values: list[object] | None = None,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
) -> MethodParameterCatalog:
    return MethodParameterCatalog.model_validate(
        {
            "catalog_id": "parameter_validation",
            "method_id": "assembly.test.parameter_validation",
            "tool": "test-tool",
            "parameters": {
                "value": {
                    "value_type": value_type,
                    "application": {"kind": "stage_value", "key": "value"},
                    "layers": {"l2": {"adjustable": True}},
                    "allowed_values": allowed_values,
                    "min": minimum,
                    "max": maximum,
                }
            },
        }
    )


@pytest.mark.parametrize(
    ("value_type", "value", "message"),
    [
        ("int", True, "cannot be rendered as int"),
        ("int", 1.0, "cannot be rendered as int"),
        ("float", True, "cannot be rendered as float"),
        ("float", float("nan"), "finite float"),
        ("float", float("inf"), "finite float"),
        ("float", float("-inf"), "finite float"),
        ("boolean", 1, "cannot be rendered as boolean"),
        ("string", "", "cannot be rendered as string"),
        ("string", "   ", "cannot be rendered as string"),
        ("string", 1, "cannot be rendered as string"),
        ("path", "", "cannot be rendered as path"),
        ("path", "   ", "cannot be rendered as path"),
        ("path", Path("reads.fastq.gz"), "cannot be rendered as path"),
        ("path_list", "reads.fastq.gz", "cannot be rendered as path_list"),
        ("path_list", (), "cannot be rendered as path_list"),
        ("path_list", [], "cannot be rendered as path_list"),
        ("path_list", [""], "cannot be rendered as path_list"),
        ("path_list", ["   "], "cannot be rendered as path_list"),
        ("path_list", ["reads.fastq.gz", 1], "cannot be rendered as path_list"),
    ],
)
def test_resolver_rejects_values_with_wrong_catalog_shape(
    value_type: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_parameters(
            _l2_catalog(value_type),
            l0_executor_values={},
            snapshot=_assembly_snapshot(),
            approved_l2={"value": value},
        )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (9, "below minimum 10"),
        (21, "above maximum 20"),
    ],
)
def test_resolver_rejects_catalog_bounds(value: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_parameters(
            _l2_catalog("int", minimum=10, maximum=20),
            l0_executor_values={},
            snapshot=_assembly_snapshot(),
            approved_l2={"value": value},
        )


def test_resolver_rejects_values_outside_catalog_allowed_values() -> None:
    with pytest.raises(ValueError, match="not in allowed_values"):
        resolve_parameters(
            _l2_catalog("string", allowed_values=["nano-raw", "nano-hq"]),
            l0_executor_values={},
            snapshot=_assembly_snapshot(),
            approved_l2={"value": "pacbio-raw"},
        )


def test_resolver_accepts_valid_boundaries_without_coercion() -> None:
    for value in (10, 20):
        resolved = resolve_parameters(
            _l2_catalog("float", minimum=10, maximum=20),
            l0_executor_values={},
            snapshot=_assembly_snapshot(),
            approved_l2={"value": value},
        )
        assert resolved.effective_parameters["value"] is value


def test_resolver_preserves_sparse_optional_l2_omission() -> None:
    resolved = resolve_parameters(
        _l2_catalog("int", minimum=1),
        l0_executor_values={},
        snapshot=_assembly_snapshot(),
        approved_l2={},
    )

    assert resolved.l2_approved == {}
    assert resolved.effective_parameters == {}


def test_resolver_validates_selected_l0_value() -> None:
    catalog = MethodParameterCatalog.model_validate(
        {
            "catalog_id": "parameter_validation",
            "method_id": "assembly.test.parameter_validation",
            "tool": "test-tool",
            "parameters": {
                "input_reads": {
                    "value_type": "path_list",
                    "application": {"kind": "positional", "position": 0},
                    "layers": {
                        "l0": {
                            "kind": "runtime",
                            "binding": "assembly_input_reads",
                        }
                    },
                }
            },
        }
    )

    with pytest.raises(ValueError, match="resolved from L0"):
        resolve_parameters(
            catalog,
            l0_executor_values={"assembly_input_reads": "reads.fastq.gz"},
            snapshot=_assembly_snapshot(),
            approved_l2={},
        )


def test_resolver_rejects_supplied_l0_binding_not_declared_by_catalog() -> None:
    catalog = load_parameter_catalog("assembly.candidate.hifiasm")

    with pytest.raises(ValueError, match="assembly_method_read_type.*not declared"):
        resolve_parameters(
            catalog,
            l0_executor_values={
                "assembly_method_read_type": "hifi",
                "assembly_output_prefix": "work/hifiasm/out",
                "assembly_input_reads": ["reads.fastq.gz"],
            },
            snapshot=_assembly_snapshot("assembly.candidate.hifiasm"),
            approved_l2={},
        )


def test_resolver_validates_selected_l1_value() -> None:
    catalog = MethodParameterCatalog.model_validate(
        {
            "catalog_id": "parameter_validation",
            "method_id": "assembly.test.parameter_validation",
            "tool": "test-tool",
            "parameters": {
                "genome_size_path": {
                    "value_type": "path",
                    "application": {"kind": "stage_value", "key": "genome_size_path"},
                    "layers": {
                        "l1": {
                            "fact": "effective_genome_size_bp",
                            "transform": "identity",
                        }
                    },
                }
            },
        }
    )

    with pytest.raises(ValueError, match="resolved from L1"):
        resolve_parameters(
            catalog,
            l0_executor_values={},
            snapshot=_assembly_snapshot(),
            approved_l2={},
        )


@pytest.mark.parametrize(
    ("catalog", "snapshot", "l0_values", "approved_l2", "message"),
    [
        (
            load_parameter_catalog("assembly.candidate.flye"),
            _assembly_snapshot("assembly.candidate.flye"),
            {
                "assembly_method_read_type": "nano-hq",
                "assembly_input_reads": ["reads.fastq.gz"],
                "assembly_output_dir": "work/flye",
                "assembly_threads": 8,
            },
            {"asm_coverage": 201},
            "above maximum 200",
        ),
        (
            load_autohic_parameter_catalog(),
            _autohic_snapshot(),
            {},
            {
                "restriction_enzyme": "MboI",
                "error_filter_score": float("inf"),
            },
            "finite float",
        ),
        (
            load_t2t_parameter_catalog(),
            _t2t_snapshot(),
            {},
            {"min_mapq": True},
            "cannot be rendered as int",
        ),
    ],
    ids=("assembly", "autohic", "t2t"),
)
def test_shared_resolver_enforces_catalog_values_across_stages(
    catalog: MethodParameterCatalog,
    snapshot: RecommendationInputSnapshotV3,
    l0_values: dict[str, object],
    approved_l2: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_parameters(
            catalog,
            l0_executor_values=l0_values,
            snapshot=snapshot,
            approved_l2=approved_l2,
        )
