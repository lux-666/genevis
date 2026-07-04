"""Build full and prompt-scoped profiles for assembly parameter recommendation."""

from __future__ import annotations

from typing import Any, Literal

from app.schemas.state import TopLevelState
from app.stages.assembly.method_application.lifecycle import assembly_output

ParameterPromptTask = Literal[
    "recommend_initial",
    "retry_after_execution_failure",
    "retry_after_review_failure",
]

PROFILE_SECTIONS = (
    "sample",
    "assets",
    "read_qc",
    "genome_survey",
    "previous_attempt",
    "assembly_review",
)

INITIAL_SAMPLE_FIELDS = (
    "sample_id",
    "organism",
    "taxon_id",
    "clade",
    "ploidy",
    "expected_chromosome_count",
    "target_level",
)
PROMPT_ASSET_FIELDS = (
    "read_type",
    "read_format",
    "has_hifi",
    "has_ont",
    "has_ultra_long",
    "has_hic",
    "has_trio",
    "has_rnaseq",
)
EXECUTION_REPAIR_FIELDS = (
    "attempt_id",
    "method_id",
    "tool_version",
    "params",
    "command_argv",
    "selected_cases",
    "exit_code",
    "failure_class",
    "stderr_tail",
    "stdout_tail",
)
QUALITY_REPLAN_PREVIOUS_FIELDS = (
    "attempt_id",
    "method_id",
    "tool_version",
    "params",
    "command_argv",
    "selected_cases",
)
QUALITY_REVIEW_FIELDS = (
    "assembly_size",
    "assembly_size_ratio",
    "contig_count",
    "contig_n50",
    "contig_n90",
    "largest_contig",
    "busco_complete",
    "busco_single",
    "busco_duplicate",
    "busco_fragmented",
    "busco_missing",
    "quast_metrics",
)


def build_full_parameter_profile(
    state: TopLevelState,
    *,
    assembly_out: dict[str, Any] | None = None,
    previous_attempt: dict[str, Any] | None = None,
    assembly_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the complete replay/audit profile for parameter recommendation."""

    assembly_out = assembly_out or assembly_output(state)
    stage_outputs = state.get("stage_outputs") or {}
    return {
        "sample": _sample_profile(state, assembly_out),
        "assets": _assets_profile(state, assembly_out),
        "read_qc": _read_qc_profile(stage_outputs.get("read_qc") or {}),
        "genome_survey": _genome_survey_profile(stage_outputs.get("genome_survey") or {}),
        "previous_attempt": _previous_attempt_profile(previous_attempt or assembly_out.get("previous_parameter_attempt") or {}),
        "assembly_review": _assembly_review_profile(assembly_review or stage_outputs.get("assembly_review") or {}),
    }


def build_prompt_profile(full_profile: dict[str, Any], task: ParameterPromptTask) -> dict[str, Any]:
    """Return the task-scoped profile section intended for LLM prompting."""

    if task == "recommend_initial":
        return {
            "sample": _pick(full_profile.get("sample") or {}, INITIAL_SAMPLE_FIELDS),
            "assets": _pick(full_profile.get("assets") or {}, PROMPT_ASSET_FIELDS),
            "read_qc": _drop_empty(full_profile.get("read_qc") or {}),
            "genome_survey": _drop_empty(full_profile.get("genome_survey") or {}),
        }
    if task == "retry_after_execution_failure":
        return {
            "sample": _pick(full_profile.get("sample") or {}, ("sample_id", "organism", "clade", "target_level")),
            "assets": _pick(full_profile.get("assets") or {}, PROMPT_ASSET_FIELDS),
            "previous_attempt": _pick(full_profile.get("previous_attempt") or {}, EXECUTION_REPAIR_FIELDS),
        }
    if task == "retry_after_review_failure":
        return {
            "sample": _pick(full_profile.get("sample") or {}, INITIAL_SAMPLE_FIELDS),
            "previous_attempt": _pick(full_profile.get("previous_attempt") or {}, QUALITY_REPLAN_PREVIOUS_FIELDS),
            "assembly_review": _pick(full_profile.get("assembly_review") or {}, QUALITY_REVIEW_FIELDS),
        }
    raise ValueError(f"Unknown parameter prompt task: {task}")


def flatten_profile_facts(full_profile: dict[str, Any]) -> dict[str, Any]:
    """Flatten a full profile into dotted facts for rule validation."""

    facts: dict[str, Any] = {}

    def visit(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_prefix = f"{prefix}.{key}" if prefix else str(key)
                visit(child_prefix, child)
            return
        if value is None:
            return
        facts[prefix] = value
        leaf = prefix.rsplit(".", 1)[-1]
        facts.setdefault(leaf, value)

    visit("", full_profile)
    return facts


def _sample_profile(state: TopLevelState, assembly_out: dict[str, Any]) -> dict[str, Any]:
    input_profile = dict(assembly_out.get("input_profile") or {})
    shared = state.get("shared_parameters") or {}
    return _drop_empty(
        {
            "sample_id": _first_value(state.get("session_id"), _lookup(shared, "sample_id"), _lookup(input_profile, "sample_id")),
            "organism": _first_value(_lookup(shared, "organism"), _lookup(input_profile, "organism")),
            "taxon_id": _first_value(_lookup(shared, "taxon_id"), _lookup(input_profile, "taxon_id")),
            "clade": _first_value(_lookup(shared, "clade"), _lookup(input_profile, "clade")),
            "ploidy": _first_value(_lookup(shared, "ploidy"), _lookup(input_profile, "ploidy")),
            "expected_chromosome_count": _first_value(
                _lookup(shared, "expected_chromosome_count"),
                _lookup(input_profile, "expected_chromosome_count"),
            ),
            "target_level": _first_value(_lookup(shared, "target_level"), _lookup(input_profile, "target_level")),
        }
    )


def _assets_profile(state: TopLevelState, assembly_out: dict[str, Any]) -> dict[str, Any]:
    input_profile = dict(assembly_out.get("input_profile") or {})
    input_ref = assembly_out.get("input_ref") or input_profile.get("primary_long_reads_ref")
    return _drop_empty(
        {
            "read_type": _first_value(assembly_out.get("assembly_route"), input_profile.get("recommended_route")),
            "read_format": _infer_read_format(input_ref),
            "input_files": _input_files(input_ref),
            "has_hifi": _first_value(input_profile.get("has_hifi"), bool(assembly_out.get("hifi_reads_ref"))),
            "has_ont": _first_value(input_profile.get("has_ont"), bool(assembly_out.get("ont_reads_ref"))),
            "has_ultra_long": _first_value(input_profile.get("has_ultra_long"), bool(assembly_out.get("ultra_long_reads_ref"))),
            "has_hic": _first_value(input_profile.get("has_hic"), bool(assembly_out.get("hic_reads_ref"))),
            "has_trio": _first_value(input_profile.get("has_trio"), bool(assembly_out.get("trio_refs"))),
            "has_rnaseq": _first_value(input_profile.get("has_rnaseq"), bool((state.get("stage_outputs") or {}).get("rnaseq"))),
        }
    )


def _read_qc_profile(read_qc: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty(
        {
            "read_count": _first_path(read_qc, "read_count", "summary.read_count", "long_read_summary.read_count"),
            "total_bases": _first_path(read_qc, "total_bases", "summary.total_bases", "long_read_summary.total_bases"),
            "read_n50": _first_path(read_qc, "read_n50", "n50", "summary.n50", "long_read_summary.n50"),
            "read_n90": _first_path(read_qc, "read_n90", "n90", "summary.n90", "long_read_summary.n90"),
            "read_mean": _first_path(read_qc, "read_mean", "mean_read_length", "summary.mean_read_length"),
            "read_median": _first_path(read_qc, "read_median", "median_read_length", "summary.median_read_length"),
            "longest_read": _first_path(read_qc, "longest_read", "max_read_length", "summary.max_read_length"),
            "bases_above_1kb": _first_path(read_qc, "bases_above_1kb", "summary.bases_above_1kb"),
            "bases_above_5kb": _first_path(read_qc, "bases_above_5kb", "summary.bases_above_5kb"),
            "bases_above_10kb": _first_path(read_qc, "bases_above_10kb", "summary.bases_above_10kb"),
            "bases_above_50kb": _first_path(read_qc, "bases_above_50kb", "summary.bases_above_50kb"),
            "mean_quality": _first_path(read_qc, "mean_quality", "summary.mean_quality"),
            "q10_fraction": _first_path(read_qc, "q10_fraction", "summary.q10_fraction"),
            "q20_fraction": _first_path(read_qc, "q20_fraction", "summary.q20_fraction"),
            "effective_coverage_1kb": _first_path(read_qc, "effective_coverage_1kb", "summary.effective_coverage_1kb"),
            "effective_coverage_5kb": _first_path(read_qc, "effective_coverage_5kb", "summary.effective_coverage_5kb"),
            "gc_content": _first_path(read_qc, "gc_content", "summary.gc_content"),
            "adapter_fraction": _first_path(read_qc, "adapter_fraction", "summary.adapter_fraction"),
            "contamination_hint": _first_path(read_qc, "contamination_hint", "summary.contamination_hint"),
            "quality_available": _first_path(read_qc, "quality_available", "summary.quality_available"),
        }
    )


def _genome_survey_profile(genome_survey: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty(
        {
            "estimated_genome_size": _first_path(genome_survey, "estimated_genome_size", "genome_size_prior", "estimates.genome_size"),
            "genome_size_confidence": _first_path(genome_survey, "genome_size_confidence", "confidence"),
            "heterozygosity": _first_path(genome_survey, "heterozygosity", "estimates.heterozygosity"),
            "repeat_content": _first_path(genome_survey, "repeat_content", "estimates.repeat_content", "estimates.repeat_fraction"),
            "gc_content": _first_path(genome_survey, "gc_content", "estimates.gc_content"),
            "kmer_peak_coverage": _first_path(genome_survey, "kmer_peak_coverage", "estimates.kmer_peak_coverage", "estimates.hom_cov"),
            "kmer_histogram_shape": _first_path(genome_survey, "kmer_histogram_shape", "estimates.kmer_histogram_shape"),
            "estimated_error_rate": _first_path(genome_survey, "estimated_error_rate", "estimates.error_rate"),
            "ploidy_hint": _first_path(genome_survey, "ploidy_hint", "estimates.ploidy", "estimates.ploidy_hint"),
        }
    )


def _previous_attempt_profile(previous_attempt: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty(
        {
            "attempt_id": previous_attempt.get("attempt_id"),
            "method_id": previous_attempt.get("method_id"),
            "tool_version": previous_attempt.get("tool_version"),
            "params": _first_value(previous_attempt.get("params"), previous_attempt.get("recommended_params")),
            "command_argv": _first_value(previous_attempt.get("command_argv"), previous_attempt.get("argv")),
            "selected_cases": _first_value(previous_attempt.get("selected_cases"), previous_attempt.get("selected_case_ids")),
            "exit_code": _first_path(previous_attempt, "exit_code", "execution_result.return_code", "execution_result.exit_code"),
            "failure_class": _first_value(previous_attempt.get("failure_class"), previous_attempt.get("failure_mode")),
            "stderr_tail": previous_attempt.get("stderr_tail"),
            "stdout_tail": previous_attempt.get("stdout_tail"),
        }
    )


def _assembly_review_profile(assembly_review: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(assembly_review.get("metrics") or assembly_review.get("summary") or {})
    return _drop_empty(
        {
            "assembly_size": _first_path(assembly_review, "assembly_size", "metrics.assembly_size", "summary.assembly_size"),
            "assembly_size_ratio": _first_path(assembly_review, "assembly_size_ratio", "metrics.assembly_size_ratio"),
            "contig_count": _first_path(assembly_review, "contig_count", "metrics.contig_count"),
            "contig_n50": _first_path(assembly_review, "contig_n50", "metrics.contig_n50", "metrics.n50"),
            "contig_n90": _first_path(assembly_review, "contig_n90", "metrics.contig_n90", "metrics.n90"),
            "largest_contig": _first_path(assembly_review, "largest_contig", "metrics.largest_contig"),
            "busco_complete": _first_path(assembly_review, "busco_complete", "metrics.busco_complete"),
            "busco_single": _first_path(assembly_review, "busco_single", "metrics.busco_single"),
            "busco_duplicate": _first_path(assembly_review, "busco_duplicate", "metrics.busco_duplicate"),
            "busco_fragmented": _first_path(assembly_review, "busco_fragmented", "metrics.busco_fragmented"),
            "busco_missing": _first_path(assembly_review, "busco_missing", "metrics.busco_missing"),
            "quast_metrics": _first_value(assembly_review.get("quast_metrics"), metrics.get("quast_metrics")),
        }
    )


def _pick(values: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return _drop_empty({key: values.get(key) for key in keys})


def _drop_empty(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _first_path(values: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value = _lookup(values, path)
        if value is not None:
            return value
    return None


def _lookup(values: Any, path: str) -> Any:
    current = values
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current


def _input_files(input_ref: Any) -> list[str] | None:
    if input_ref is None:
        return None
    if isinstance(input_ref, list):
        return [str(item) for item in input_ref]
    return [str(input_ref)]


def _infer_read_format(input_ref: Any) -> str | None:
    files = _input_files(input_ref)
    if not files:
        return None
    first = files[0].lower()
    if first.endswith((".fastq", ".fq", ".fastq.gz", ".fq.gz")):
        return "fastq"
    if first.endswith((".fasta", ".fa", ".fna", ".fasta.gz", ".fa.gz", ".fna.gz")):
        return "fasta"
    if first.endswith(".fofn"):
        return "fofn"
    return None
