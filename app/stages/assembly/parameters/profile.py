"""Build full and prompt-scoped profiles for assembly parameter recommendation."""

from __future__ import annotations

from typing import Any

from app.domains.assembly.selection import resolve_parameter_case_read_type
from app.schemas.artifacts import ParameterAttemptTask
from app.schemas.state import TopLevelState
from app.stages.base import stage_output


INITIAL_SAMPLE_FIELDS = (
    "organism",
    "taxon_id",
    "clade",
    "ploidy",
    "expected_chromosome_count",
    "target_level",
)
PROMPT_ASSET_FIELDS = (
    "assembly_route",
    "read_type",
    "read_format",
    "has_hifi",
    "has_ont",
    "has_hic",
    "has_rnaseq",
)
EXECUTION_REPAIR_FIELDS = (
    "tool_version",
    "parameters",
    "exit_code",
    "failure_class",
    "execution_status",
    "execution_status_detail",
    "resource_facts",
)
QUALITY_REPLAN_PREVIOUS_FIELDS = (
    "tool_version",
    "parameters",
)
QUALITY_REVIEW_FIELDS = (
    "failure_mode",
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

    assembly_out = assembly_out or stage_output(state, "assembly")
    stage_outputs = state.get("stage_outputs") or {}
    genome_survey = _genome_survey_profile(stage_outputs.get("genome_survey") or {})
    return {
        "sample": _sample_profile(state, assembly_out),
        "assets": _assets_profile(assembly_out),
        "read_qc": _read_qc_profile(
            stage_outputs.get("read_qc") or {},
            genome_survey=genome_survey,
        ),
        "genome_survey": genome_survey,
        "previous_attempt": _previous_attempt_profile(previous_attempt or assembly_out.get("previous_parameter_attempt") or {}),
        "assembly_review": _assembly_review_profile(assembly_review or stage_outputs.get("assembly_review") or {}),
    }


def build_prompt_profile(
    full_profile: dict[str, Any],
    task: ParameterAttemptTask,
) -> dict[str, Any]:
    """Return explicitly allowed task facts for the Assembly request builder."""

    if task == "recommend_initial":
        return {
            "sample": _pick(full_profile.get("sample") or {}, INITIAL_SAMPLE_FIELDS),
            "assets": _pick(full_profile.get("assets") or {}, PROMPT_ASSET_FIELDS),
            "read_qc": _drop_empty(full_profile.get("read_qc") or {}),
            "genome_survey": _drop_empty(full_profile.get("genome_survey") or {}),
        }
    if task == "retry_after_execution_failure":
        return {
            "sample": _pick(
                full_profile.get("sample") or {},
                ("organism", "clade", "target_level"),
            ),
            "assets": _pick(full_profile.get("assets") or {}, PROMPT_ASSET_FIELDS),
            "previous_attempt": _pick(
                full_profile.get("previous_attempt") or {},
                EXECUTION_REPAIR_FIELDS,
            ),
        }
    if task == "retry_after_review_failure":
        return {
            "sample": _pick(full_profile.get("sample") or {}, INITIAL_SAMPLE_FIELDS),
            "previous_attempt": _pick(
                full_profile.get("previous_attempt") or {},
                QUALITY_REPLAN_PREVIOUS_FIELDS,
            ),
            "assembly_review": _pick(
                full_profile.get("assembly_review") or {},
                QUALITY_REVIEW_FIELDS,
            ),
        }
    raise ValueError(f"Unknown parameter prompt task: {task}")


def _sample_profile(state: TopLevelState, assembly_out: dict[str, Any]) -> dict[str, Any]:
    input_profile = dict(assembly_out.get("input_profile") or {})
    shared = state.get("shared_parameters") or {}
    return _drop_empty(
        {
            "sample_id": _first_value(
                _lookup(shared, "sample_id"),
                _lookup(input_profile, "sample_id"),
            ),
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


def _assets_profile(assembly_out: dict[str, Any]) -> dict[str, Any]:
    input_profile = dict(assembly_out.get("input_profile") or {})
    input_ref = assembly_out.get("input_ref") or input_profile.get("primary_long_reads_ref")
    selection = dict(assembly_out.get("assembly_selection") or {})
    assembly_route = _first_value(assembly_out.get("assembly_route"), input_profile.get("recommended_route"))
    return _drop_empty(
        {
            "assembly_route": assembly_route,
            "read_type": _parameter_case_read_type(selection, assembly_route, assembly_out, input_profile),
            "read_format": _infer_read_format(input_ref),
            "input_files": _input_files(input_ref),
            "has_hifi": _first_value(input_profile.get("has_hifi"), bool(assembly_out.get("hifi_reads_ref"))),
            "has_ont": _first_value(input_profile.get("has_ont"), bool(assembly_out.get("ont_reads_ref"))),
            "has_hic": _first_value(input_profile.get("has_hic"), bool(assembly_out.get("hic_reads_ref"))),
            "has_rnaseq": bool(input_profile.get("has_rnaseq")),
        }
    )


def _parameter_case_read_type(
    selection: dict[str, Any],
    route: Any,
    assembly_out: dict[str, Any],
    input_profile: dict[str, Any],
) -> str | None:
    return resolve_parameter_case_read_type(
        ("assembly_selection.read_type", selection.get("read_type")),
        ("assembly_out.read_type", assembly_out.get("read_type")),
        ("input_profile.read_type", input_profile.get("read_type")),
        ("input_profile.primary_read_type", input_profile.get("primary_read_type")),
        ("assembly_route", route),
        context="assembly.parameter_profile.assets",
    )


def _read_qc_profile(
    read_qc: dict[str, Any],
    *,
    genome_survey: dict[str, Any],
) -> dict[str, Any]:
    """Normalize the current seqkit summary contract for recommendation."""

    summary = read_qc.get("summary") or {}
    total_bases = summary.get("total_bases")
    profile = _drop_empty(
        {
            "read_count": summary.get("num_seqs"),
            "total_bases": total_bases,
            "read_n50": summary.get("n50"),
            "read_mean": summary.get("avg_len"),
            "longest_read": summary.get("max_len"),
            "q20_fraction": _percent_to_fraction(summary.get("q20_percent")),
            "mean_quality": summary.get("avg_qual"),
        }
    )

    effective_genome_size = genome_survey.get("effective_genome_size")
    if _is_positive_number(total_bases) and _is_positive_number(effective_genome_size):
        profile["raw_coverage"] = float(total_bases) / float(effective_genome_size)
        profile["raw_coverage_basis"] = _drop_empty(
            {
                "numerator_bases": total_bases,
                "numerator_source_path": "read_qc.summary.total_bases",
                "denominator_bases": effective_genome_size,
                "denominator_source": genome_survey.get("genome_size_source"),
                "denominator_source_path": genome_survey.get("genome_size_source_path"),
            }
        )

    return profile


def _genome_survey_profile(genome_survey: dict[str, Any]) -> dict[str, Any]:
    """Normalize effective values and their measured/provided provenance."""

    estimates = genome_survey.get("estimates") or {}
    measured = genome_survey.get("measured_estimates") or {}
    provided = genome_survey.get("provided_overrides") or {}
    sources = genome_survey.get("estimate_sources") or {}
    source_paths = genome_survey.get("estimate_source_paths") or {}

    effective_genome_size = estimates.get("genome_size")
    measured_genome_size = measured.get("genome_size")
    provided_genome_size = provided.get("genome_size")
    genome_size_source = sources.get("genome_size")
    genome_size_source_path = source_paths.get("genome_size")
    if genome_size_source == "survey" and genome_size_source_path is None:
        genome_size_source_path = "genome_survey.measured_estimates.genome_size"
    elif genome_size_source == "provided" and genome_size_source_path is None:
        genome_size_source_path = "genome_survey.provided_overrides.genome_size"

    delta_ratio = None
    if _is_positive_number(measured_genome_size) and _is_positive_number(provided_genome_size):
        delta_ratio = (float(provided_genome_size) - float(measured_genome_size)) / float(measured_genome_size)

    return _drop_empty(
        {
            "effective_genome_size": effective_genome_size,
            "measured_genome_size": measured_genome_size,
            "provided_genome_size": provided_genome_size,
            "genome_size_source": genome_size_source,
            "genome_size_source_path": genome_size_source_path,
            "genome_size_delta_ratio": delta_ratio,
            "heterozygosity": estimates.get("heterozygosity"),
            "repeat_fraction": estimates.get("repeat_fraction"),
            "confidence": genome_survey.get("confidence"),
        }
    )


def _percent_to_fraction(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > 100:
        return None
    return float(value) / 100.0


def _is_positive_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0


def _previous_attempt_profile(previous_attempt: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty(
        {
            "attempt_id": previous_attempt.get("attempt_id"),
            "method_id": previous_attempt.get("method_id"),
            "tool_version": previous_attempt.get("tool_version"),
            "parameters": _first_value(
                previous_attempt.get("parameters"),
                previous_attempt.get("recommended_params"),
            ),
            "command_argv": _first_value(previous_attempt.get("argv"), previous_attempt.get("command_argv")),
            "literature_case_ids": previous_attempt.get("selected_case_ids"),
            "exit_code": _first_path(previous_attempt, "exit_code", "execution_result.return_code", "execution_result.exit_code"),
            "failure_class": _first_path(
                previous_attempt,
                "failure_class",
                "failure_mode",
                "execution_result.failure_class",
            ),
            "execution_status": _first_path(previous_attempt, "execution_result.status"),
            "execution_status_detail": _first_path(previous_attempt, "execution_result.status_detail"),
            "stderr_ref": _first_path(previous_attempt, "stderr_ref", "execution_result.stderr_ref"),
            "stdout_ref": _first_path(previous_attempt, "stdout_ref", "execution_result.stdout_ref"),
            "resource_facts": _first_path(
                previous_attempt,
                "resource_facts",
                "execution_result.resource_facts",
                "execution_result.resources",
            ),
            "stderr_tail": previous_attempt.get("stderr_tail"),
            "stdout_tail": previous_attempt.get("stdout_tail"),
        }
    )


def _assembly_review_profile(assembly_review: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(assembly_review.get("metrics") or assembly_review.get("summary") or {})
    return _drop_empty(
        {
            "failure_mode": assembly_review.get("failure_mode"),
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


def _drop_empty(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _pick(values: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return _drop_empty({key: values.get(key) for key in keys})


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
