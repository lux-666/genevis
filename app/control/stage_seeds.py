"""Project frozen run configuration into immutable per-stage seeds."""

from __future__ import annotations

from typing import Any

from app.review.gates import registered_review_stages
from app.schemas.run_config import RunConfig
from app.schemas.shared_parameters import SharedParameters
from app.schemas.state import StageOutputs, validate_stage_outputs


def stage_seeds_from_run_config(
    run_config: RunConfig,
    *,
    shared_parameters: SharedParameters | dict[str, Any] | None,
    dependency_resolver: Any | None = None,
    species_name: str | None = None,
) -> StageOutputs:
    """Project shared stage configuration without occurrence-local consent.

    ``allow_local_execution`` belongs to one workflow occurrence and is applied
    only when that occurrence is entered.  Keeping it out of these immutable
    per-stage seeds prevents one locally authorized occurrence from granting
    process execution to another occurrence of the same stage.
    """

    requested = set(run_config.requested_stage_names())
    seeds: StageOutputs = {}

    if "read_qc" in requested and run_config.read_qc:
        read_qc_seed: dict[str, Any] = {}
        if run_config.read_qc.length_filter:
            filter_config = run_config.read_qc.length_filter
            filter_request = filter_config.model_dump(mode="json")
            filter_request["method"] = filter_config.selected_method()
            read_qc_seed["length_filter_request"] = filter_request
        if run_config.read_qc.short_reads:
            read_qc_seed["short_reads_request"] = (
                run_config.read_qc.short_reads.model_dump(
                    mode="json", exclude_none=True
                )
            )
        if run_config.read_qc.hic_reads:
            read_qc_seed["hic_reads_request"] = run_config.read_qc.hic_reads.model_dump(
                mode="json", exclude_none=True
            )
        if read_qc_seed:
            seeds["read_qc"] = read_qc_seed

    if (
        "genome_survey" in requested
        and run_config.genome_survey
        and run_config.genome_survey.survey_parameters
    ):
        survey_parameters = run_config.genome_survey.survey_parameters.model_dump(
            exclude_none=True
        )
        species_facts = (
            run_config.shared_parameters.species_facts
            if run_config.shared_parameters is not None
            else None
        )
        survey_parameters["ploidy"] = (
            species_facts.ploidy
            if species_facts is not None and species_facts.ploidy is not None
            else 2
        )
        seeds["genome_survey"] = {"survey_parameters": survey_parameters}

    if "assembly" in requested and run_config.assembly:
        assembly_seed: dict[str, Any] = {}
        for key in (
            "case_source",
            "threads",
            "hifi_reads_ref",
            "hic_reads_ref",
            "hifiasm_inputs",
            "target_level",
            "assembly_route",
            "method_route_advisor",
            "parameter_advisor",
        ):
            value = getattr(run_config.assembly, key)
            if value is not None:
                assembly_seed[key] = (
                    value.model_dump(mode="json", exclude_none=True)
                    if key == "hifiasm_inputs"
                    else value
                )
        if assembly_seed:
            seeds["assembly"] = assembly_seed

    if "assembly_dedup" in requested and run_config.assembly_dedup:
        seeds["assembly_dedup"] = run_config.assembly_dedup.model_dump(mode="json")

    if "assembly_review" in requested:
        review_seed: dict[str, Any] = {}
        if run_config.assembly_review and run_config.assembly_review.quast:
            review_seed["quast_parameters"] = (
                run_config.assembly_review.quast.model_dump()
            )
        if dependency_resolver is None:
            raise ValueError(
                "assembly_review seed requires the frozen database contract"
            )
        from app.execution.database_resolution import validate_review_databases

        review_seed.update(
            validate_review_databases(
                run_config,
                dependency_resolver,
                species_name=species_name,
            )
        )
        seeds["assembly_review"] = review_seed

    if "hic_scaffolding" in requested and run_config.hic_scaffolding:
        hic_seed: dict[str, Any] = {}
        hic_seed["parameter_advisor"] = run_config.hic_scaffolding.parameter_advisor
        if run_config.hic_scaffolding.autohic_parameters:
            hic_seed["autohic_parameters"] = (
                run_config.hic_scaffolding.autohic_parameters.model_dump(
                    exclude_none=True
                )
            )
        hic_seed["parameter_retry_policy"] = {
            "retry_on_execution_failure": (
                run_config.hic_scaffolding.retry_on_execution_failure
            ),
            "max_execution_retries": run_config.hic_scaffolding.max_execution_retries,
            "max_review_retries": run_config.hic_scaffolding.max_review_retries,
        }
        seeds["hic_scaffolding"] = hic_seed

    from app.schemas.run_config import (
        BasePolishRunConfig,
        GapCloserRunConfig,
        T2TRunConfig,
    )

    for stage, config_type in (
        ("t2t", T2TRunConfig),
        ("gapcloser", GapCloserRunConfig),
        ("base_polish", BasePolishRunConfig),
    ):
        if stage in requested:
            config = getattr(run_config, stage) or config_type()
            seeds[stage] = config.model_dump(mode="json", exclude_none=True)
            if (
                stage == "t2t"
                and config.recommendation_mode == "advisor"
                and "mode" not in seeds[stage]
            ):
                seeds[stage]["mode"] = "auto_patch"

    for review_stage in registered_review_stages():
        if review_stage not in requested:
            continue
        stage_seed = dict(seeds.get(review_stage) or {})
        stage_seed["review_policy"] = {
            "max_blocked_seconds": (
                run_config.review_policy.effective_max_blocked_seconds(review_stage)
            )
        }
        seeds[review_stage] = stage_seed

    return validate_stage_outputs(seeds)
