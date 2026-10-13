"""Anti-corruption boundary: build CanonicalRecommendationFacts from upstream mappings.

Read-QC publishes a summary mapping and GenomeSurveyResult.estimates /
measured_estimates / provided_overrides are dict[str, Any].  This adapter
allowlists the currently authoritative producer keys, validates them into
private strict source models, and constructs canonical facts.  No downstream
recommendation code receives the source mappings.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal

from app.schemas.parameter_recommendation import (
    CanonicalAssetPresence,
    CanonicalGenomeFacts,
    CanonicalReadFacts,
    CanonicalRecommendationFacts,
    CanonicalSequencingFacts,
    CanonicalSpeciesFacts,
    QuantityFact,
    QuantityOrigin,
)

# ---------------------------------------------------------------------------
# Private adapter-local source models (never exposed downstream)
# ---------------------------------------------------------------------------


class _ReadQCRecommendationSource:
    """Validated projection of the Read-QC summary mapping."""

    __slots__ = (
        "read_count",
        "total_bases_bp",
        "read_n50_bp",
        "mean_read_length_bp",
        "longest_read_bp",
        "q20_fraction",
        "mean_quality",
    )

    def __init__(self, summary: dict[str, Any]) -> None:
        self.read_count: int | None = _pos_int_or_none(
            summary.get("num_seqs"),
            "read_qc.summary.num_seqs",
        )
        self.total_bases_bp: int | None = _pos_int_or_none(
            summary.get("total_bases"),
            "read_qc.summary.total_bases",
        )
        self.read_n50_bp: int | None = _pos_int_or_none(
            summary.get("n50"),
            "read_qc.summary.n50",
        )
        self.mean_read_length_bp: float | None = _pos_float_or_none(
            summary.get("avg_len"),
            "read_qc.summary.avg_len",
        )
        self.longest_read_bp: int | None = _pos_int_or_none(
            summary.get("max_len"),
            "read_qc.summary.max_len",
        )
        self.q20_fraction: float | None = _percent_to_fraction(
            summary.get("q20_percent"),
            "read_qc.summary.q20_percent",
        )
        self.mean_quality: float | None = _pos_float_or_none(
            summary.get("avg_qual"),
            "read_qc.summary.avg_qual",
            allow_zero=True,
        )


class _GenomeSurveyRecommendationSource:
    """Validated projection of the genome survey estimate mappings."""

    effective_genome_size_bp: int | None
    measured_genome_size_bp: int | None
    provided_genome_size_bp: int | None
    heterozygosity: float | None
    repeat_fraction: float | None
    estimate_confidence: Literal["high", "low", "none"] | None
    genome_size_origin: Literal["measured", "provided", "resolved"]
    effective_genome_size_producer: Literal["run_config", "genome_survey"]
    provided_genome_size_producer: Literal["run_config", "genome_survey"]

    __slots__ = (
        "effective_genome_size_bp",
        "measured_genome_size_bp",
        "provided_genome_size_bp",
        "heterozygosity",
        "repeat_fraction",
        "estimate_confidence",
        "genome_size_origin",
        "effective_genome_size_producer",
        "provided_genome_size_producer",
    )

    def __init__(
        self,
        estimates: dict[str, Any],
        measured: dict[str, Any],
        provided: dict[str, Any],
        sources: dict[str, Any],
        confidence: str | None,
        shared_parameters: dict[str, Any],
    ) -> None:
        survey_effective = _pos_int_or_none(
            estimates.get("genome_size"),
            "genome_survey.estimates.genome_size",
        )
        self.measured_genome_size_bp: int | None = _pos_int_or_none(
            measured.get("genome_size"),
            "genome_survey.measured_estimates.genome_size",
        )
        survey_provided = _pos_int_or_none(
            provided.get("genome_size"),
            "genome_survey.provided_overrides.genome_size",
        )
        shared_provided = _pos_int_or_none(
            shared_parameters.get("estimated_genome_size"),
            "shared_parameters.estimated_genome_size",
        )
        if (
            shared_provided is not None
            and survey_provided is not None
            and shared_provided != survey_provided
        ):
            raise ValueError(
                "shared_parameters.estimated_genome_size conflicts with "
                "genome_survey.provided_overrides.genome_size"
            )
        self.heterozygosity: float | None = _fraction_or_none(
            estimates.get("heterozygosity"),
            "genome_survey.estimates.heterozygosity",
        )
        self.repeat_fraction: float | None = _fraction_or_none(
            estimates.get("repeat_fraction"),
            "genome_survey.estimates.repeat_fraction",
        )
        self.estimate_confidence = _confidence_or_none(
            confidence,
            "genome_survey.confidence",
        )
        raw_source = sources.get("genome_size")
        if raw_source is not None and raw_source not in ("survey", "provided"):
            raise ValueError(
                "genome_survey.estimate_sources.genome_size must be "
                "'survey' or 'provided'"
            )
        if shared_provided is not None:
            self.effective_genome_size_bp = shared_provided
            self.provided_genome_size_bp = shared_provided
            self.genome_size_origin = "provided"
            self.effective_genome_size_producer = "run_config"
            self.provided_genome_size_producer = "run_config"
            return

        self.effective_genome_size_bp = survey_effective
        self.provided_genome_size_bp = survey_provided
        self.effective_genome_size_producer = "genome_survey"
        self.provided_genome_size_producer = "genome_survey"
        if raw_source == "survey":
            self.genome_size_origin = "measured"
        elif raw_source == "provided":
            self.genome_size_origin = "provided"
        else:
            self.genome_size_origin = "resolved"


# ---------------------------------------------------------------------------
# Public adapter entry point
# ---------------------------------------------------------------------------


def build_canonical_facts(
    *,
    shared_parameters: dict[str, Any] | None,
    read_qc_output: dict[str, Any] | None,
    genome_survey_result: dict[str, Any] | None,
    asset_flags: dict[str, bool | None] | None = None,
) -> CanonicalRecommendationFacts:
    """Construct canonical facts from authoritative upstream stage outputs.

    Malformed present values raise ``ValueError``; absent optional values
    remain ``None``.  No consumer receives the source mappings.
    """

    # --- Species facts from shared_parameters ---
    shared = _top_level_mapping_or_empty(
        shared_parameters,
        "shared_parameters",
    )
    species = _build_species_facts(shared)

    # --- Read facts from Read-QC summary ---
    rqc_summary: dict[str, Any] = {}
    read_qc = _top_level_mapping_or_empty(
        read_qc_output,
        "read_qc",
    )
    if read_qc_output is not None:
        rqc_summary = _nested_mapping_or_empty(
            read_qc,
            "summary",
            "read_qc.summary",
        )
    rqc_source = _ReadQCRecommendationSource(rqc_summary)
    reads = _build_read_facts(
        rqc_source,
        read_type=shared.get("read_type"),
    )

    # --- Genome facts from genome survey ---
    gs_source: _GenomeSurveyRecommendationSource | None = None
    survey = _top_level_mapping_or_empty(
        genome_survey_result,
        "genome_survey",
    )
    if (
        genome_survey_result is not None
        or _pos_int_or_none(
            shared.get("estimated_genome_size"),
            "shared_parameters.estimated_genome_size",
        )
        is not None
    ):
        gs_source = _GenomeSurveyRecommendationSource(
            estimates=_nested_mapping_or_empty(
                survey,
                "estimates",
                "genome_survey.estimates",
            ),
            measured=_nested_mapping_or_empty(
                survey,
                "measured_estimates",
                "genome_survey.measured_estimates",
            ),
            provided=_nested_mapping_or_empty(
                survey,
                "provided_overrides",
                "genome_survey.provided_overrides",
            ),
            sources=_nested_mapping_or_empty(
                survey,
                "estimate_sources",
                "genome_survey.estimate_sources",
            ),
            confidence=survey.get("confidence"),
            shared_parameters=shared,
        )
    genome = _build_genome_facts(gs_source)

    # --- Sequencing facts: raw_coverage derived exactly once ---
    sequencing = _build_sequencing_facts(rqc_source, gs_source)

    # --- Asset presence ---
    asset_presence = _build_asset_presence(
        _top_level_mapping_or_empty(asset_flags, "asset_flags")
    )

    return CanonicalRecommendationFacts(
        species=species,
        reads=reads,
        genome=genome,
        sequencing=sequencing,
        asset_presence=asset_presence,
    )


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------


def _build_species_facts(shared: dict[str, Any]) -> CanonicalSpeciesFacts:
    raw_species: dict[str, Any] = {}
    if shared.get("species_facts") is not None:
        raw_species = _mapping_value(
            shared["species_facts"],
            "shared_parameters.species_facts",
        )

    raw_ploidy = raw_species.get("ploidy")
    if raw_ploidy is not None:
        ploidy_val = _pos_int_or_none(
            raw_ploidy,
            "shared_parameters.species_facts.ploidy",
        )
    else:
        ploidy_val = _pos_int_or_none(
            shared.get("ploidy"),
            "shared_parameters.ploidy",
        )

    raw_chr_count = raw_species.get("expected_chromosome_count")
    if raw_chr_count is not None:
        chr_count_val = _pos_int_or_none(
            raw_chr_count,
            "shared_parameters.species_facts.expected_chromosome_count",
        )
    else:
        chr_count_val = _pos_int_or_none(
            shared.get("expected_chromosome_count"),
            "shared_parameters.expected_chromosome_count",
        )

    return CanonicalSpeciesFacts(
        scientific_name=_str_or_none(
            raw_species.get("scientific_name") or shared.get("organism")
        ),
        taxon_id=_str_or_none(raw_species.get("taxon_id") or shared.get("taxon_id")),
        clade=_str_or_none(raw_species.get("clade") or shared.get("clade")),
        ploidy=QuantityFact(
            value=ploidy_val,
            unit="count",
            origin=QuantityOrigin(kind="provided", producer="run_config"),
        )
        if ploidy_val is not None
        else None,
        expected_chromosome_count=QuantityFact(
            value=chr_count_val,
            unit="count",
            origin=QuantityOrigin(kind="provided", producer="run_config"),
        )
        if chr_count_val is not None
        else None,
        supplied_telomere_motif=_str_or_none(raw_species.get("telomere_motif")),
    )


def _build_read_facts(
    src: _ReadQCRecommendationSource,
    *,
    read_type: Any,
) -> CanonicalReadFacts:
    def _bp_fact(v: int | float | None, kind: str = "measured") -> QuantityFact | None:
        if v is None:
            return None
        return QuantityFact(
            value=int(v),
            unit="bp",
            origin=QuantityOrigin(kind=kind, producer="read_qc"),  # type: ignore[arg-type]
        )

    read_count_fact = (
        QuantityFact(
            value=src.read_count,
            unit="count",
            origin=QuantityOrigin(kind="measured", producer="read_qc"),
        )
        if src.read_count is not None
        else None
    )
    q20_fact = (
        QuantityFact(
            value=src.q20_fraction,
            unit="fraction",
            origin=QuantityOrigin(kind="measured", producer="read_qc"),
        )
        if src.q20_fraction is not None
        else None
    )
    quality_fact = (
        QuantityFact(
            value=src.mean_quality,
            unit="phred",
            origin=QuantityOrigin(kind="measured", producer="read_qc"),
        )
        if src.mean_quality is not None
        else None
    )

    return CanonicalReadFacts(
        read_type=read_type,
        read_count=read_count_fact,
        total_bases=_bp_fact(src.total_bases_bp),
        read_n50=_bp_fact(src.read_n50_bp),
        mean_read_length=_bp_fact(src.mean_read_length_bp),
        longest_read=_bp_fact(src.longest_read_bp),
        q20_fraction=q20_fact,
        mean_quality=quality_fact,
    )


def _build_genome_facts(
    src: _GenomeSurveyRecommendationSource | None,
) -> CanonicalGenomeFacts:
    if src is None:
        return CanonicalGenomeFacts()

    def _bp_fact(
        v: int | None,
        origin_kind: str,
        producer: str,
    ) -> QuantityFact | None:
        if v is None:
            return None
        return QuantityFact(
            value=v,
            unit="bp",
            origin=QuantityOrigin(kind=origin_kind, producer=producer),  # type: ignore[arg-type]
        )

    # Determine effective genome size origin
    eff_origin = src.genome_size_origin  # "measured", "provided", or "resolved"

    # Delta ratio (provided - measured) / measured
    delta_fact: QuantityFact | None = None
    if (
        src.measured_genome_size_bp is not None
        and src.provided_genome_size_bp is not None
        and src.measured_genome_size_bp > 0
    ):
        delta = (
            float(src.provided_genome_size_bp) - float(src.measured_genome_size_bp)
        ) / float(src.measured_genome_size_bp)
        delta_fact = QuantityFact(
            value=delta,
            unit="relative_change",
            origin=QuantityOrigin(kind="derived", producer="genome_survey"),
        )

    het_fact = (
        QuantityFact(
            value=src.heterozygosity,
            unit="fraction",
            origin=QuantityOrigin(kind="measured", producer="genome_survey"),
        )
        if src.heterozygosity is not None
        else None
    )
    repeat_fact = (
        QuantityFact(
            value=src.repeat_fraction,
            unit="fraction",
            origin=QuantityOrigin(kind="measured", producer="genome_survey"),
        )
        if src.repeat_fraction is not None
        else None
    )

    return CanonicalGenomeFacts(
        effective_genome_size=_bp_fact(
            src.effective_genome_size_bp,
            eff_origin,
            src.effective_genome_size_producer,
        ),
        measured_genome_size=_bp_fact(
            src.measured_genome_size_bp,
            "measured",
            "genome_survey",
        ),
        provided_genome_size=_bp_fact(
            src.provided_genome_size_bp,
            "provided",
            src.provided_genome_size_producer,
        ),
        provided_vs_measured_relative_change=delta_fact,
        heterozygosity=het_fact,
        repeat_fraction=repeat_fact,
        estimate_confidence=src.estimate_confidence,
    )


def _build_sequencing_facts(
    rqc: _ReadQCRecommendationSource,
    gs: _GenomeSurveyRecommendationSource | None,
) -> CanonicalSequencingFacts:
    """Derive raw_coverage exactly once as total_bases / effective_genome_size."""

    if (
        rqc.total_bases_bp is not None
        and gs is not None
        and gs.effective_genome_size_bp is not None
        and gs.effective_genome_size_bp > 0
    ):
        coverage = float(rqc.total_bases_bp) / float(gs.effective_genome_size_bp)
        return CanonicalSequencingFacts(
            raw_coverage=QuantityFact(
                value=coverage,
                unit="x",
                origin=QuantityOrigin(
                    kind="derived",
                    producer="parameter_recommendation",
                ),
            )
        )
    return CanonicalSequencingFacts()


def _build_asset_presence(flags: dict[str, bool | None]) -> CanonicalAssetPresence:
    return CanonicalAssetPresence(
        has_hifi=_bool_or_none(flags.get("has_hifi"), "asset_flags.has_hifi"),
        has_ont=_bool_or_none(flags.get("has_ont"), "asset_flags.has_ont"),
        has_hic=_bool_or_none(flags.get("has_hic"), "asset_flags.has_hic"),
        has_rnaseq=_bool_or_none(
            flags.get("has_rnaseq"),
            "asset_flags.has_rnaseq",
        ),
    )


# ---------------------------------------------------------------------------
# Canonical fact key resolver (exhaustive typed mapping, no reflection)
# ---------------------------------------------------------------------------


def resolve_canonical_fact(key: str, facts: CanonicalRecommendationFacts) -> Any:
    """Return the raw value for a CanonicalFactKey from the given facts.

    Returns ``None`` when the fact is absent.  Raises ``KeyError`` for unknown
    keys so callers fail closed rather than silently skipping.
    """

    if key == "effective_genome_size_bp":
        f = facts.genome.effective_genome_size
        return f.value if f is not None else None
    if key == "species_ploidy":
        f = facts.species.ploidy
        return f.value if f is not None else None
    if key == "read_n50_bp":
        f = facts.reads.read_n50
        return f.value if f is not None else None
    if key == "raw_coverage_x":
        f = facts.sequencing.raw_coverage
        return f.value if f is not None else None
    if key == "hic_provided_restriction_enzyme":
        # This fact is resolved from stage context by the stage adapter,
        # not from CanonicalRecommendationFacts directly.
        return None
    raise KeyError(f"Unknown CanonicalFactKey: {key!r}")


def apply_fact_transform(transform: str, value: Any) -> Any:
    """Apply a named transform to a raw fact value."""

    if transform == "identity":
        return value
    if transform == "hifiasm_hg_size":
        # Convert bp integer to a hifiasm-style size string, e.g. 250000000 -> "250m"
        if value is None:
            return None
        bp = int(value)
        if bp >= 1_000_000_000:
            return f"{bp / 1_000_000_000:.3g}g"
        if bp >= 1_000_000:
            return f"{bp / 1_000_000:.3g}m"
        return f"{bp / 1_000:.3g}k"
    raise ValueError(f"Unknown fact transform: {transform!r}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nested_mapping_or_empty(
    container: dict[str, Any],
    key: str,
    field: str,
) -> dict[str, Any]:
    if key not in container:
        return {}
    return _mapping_value(container[key], field)


def _top_level_mapping_or_empty(
    value: Mapping[str, Any] | None,
    field: str,
) -> dict[str, Any]:
    if value is None:
        return {}
    return _mapping_value(value, field)


def _mapping_value(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    return dict(value)


def _pos_int_or_none(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a positive integer")
    try:
        finite_value = math.isfinite(float(value))
    except OverflowError:
        finite_value = False
    if not finite_value:
        raise ValueError(f"{field} must be finite")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{field} must be an integer")
    if value <= 0:
        raise ValueError(f"{field} must be positive")
    return int(value)


def _pos_float_or_none(
    value: Any, field: str, *, allow_zero: bool = False
) -> float | None:
    if value is None:
        return None
    bound = "non-negative" if allow_zero else "positive"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a {bound} number")
    try:
        fv = float(value)
    except OverflowError:
        raise ValueError(f"{field} must be finite") from None
    if not math.isfinite(fv):
        raise ValueError(f"{field} must be finite")
    if fv < 0 or (fv == 0 and not allow_zero):
        raise ValueError(f"{field} must be {bound}")
    return fv


def _fraction_or_none(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number between 0 and 1")
    try:
        fv = float(value)
    except OverflowError:
        raise ValueError(f"{field} must be finite") from None
    if not math.isfinite(fv):
        raise ValueError(f"{field} must be finite")
    if not (0.0 <= fv <= 1.0):
        raise ValueError(f"{field} must be between 0 and 1")
    return fv


def _percent_to_fraction(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number between 0 and 100")
    try:
        fv = float(value)
    except OverflowError:
        raise ValueError(f"{field} must be finite") from None
    if not math.isfinite(fv):
        raise ValueError(f"{field} must be finite")
    if not (0.0 <= fv <= 100.0):
        raise ValueError(f"{field} must be between 0 and 100")
    return fv / 100.0


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _bool_or_none(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _confidence_or_none(
    value: Any,
    field: str,
) -> Literal["high", "low", "none"] | None:
    if value is None:
        return None
    if value == "high":
        return "high"
    if value == "low":
        return "low"
    if value == "none":
        return "none"
    raise ValueError(f"{field} must be 'high', 'low', or 'none'")
