"""Build one material query without assigning aggregate QC to arbitrary reads."""

from __future__ import annotations

from typing import Any, Mapping

from app.domains.parameter_recommendation.facts import build_canonical_facts
from app.knowledge.assembly_cases import Observation
from app.schemas.parameter_recommendation import AssemblyMaterialContext

PLATFORMS = {
    "pacbio_hifi": "hifi",
    "ont": "ont",
    "clr": "clr",
    "cycloneseq": "cyclone",
    "hic": "hic",
}


def resolved_material_context(assembly: Mapping[str, Any]) -> AssemblyMaterialContext:
    """Expose roles actually bound by Assembly; ordinary ONT never implies UL."""

    member_platforms = assembly.get("assembly_input_member_platforms") or {}
    primary_platforms = member_platforms.get("primary") or []
    return AssemblyMaterialContext(
        hifi_files=primary_platforms.count("pacbio_hifi"),
        ont_files=primary_platforms.count("ont"),
        ultralong_ont_files=len(assembly.get("ultralong_read_refs") or [])
        if assembly.get("use_ultralong_ont")
        else 0,
        hic_pairs=len(assembly.get("hic_r1_reads") or [])
        if assembly.get("use_hic_phasing")
        else 0,
        trio_maternal_files=len(assembly.get("trio_maternal_read_refs") or []),
        trio_paternal_files=len(assembly.get("trio_paternal_read_refs") or []),
        phasing_mode="trio"
        if assembly.get("trio_enabled")
        else "hic"
        if assembly.get("use_hic_phasing")
        else "none",
    )


def build_material_query(
    state: Mapping[str, Any], assembly: Mapping[str, Any]
) -> dict[str, Any]:
    outputs = state.get("stage_outputs") or {}
    prior = (state.get("workflow_steps") or [])[: state.get("workflow_step_index", 0)]
    skipped = state.get("skip_reasons") or {}
    available = {s["stage"] for s in prior if s.get("step_id") not in skipped}
    qc = dict(outputs.get("read_qc") or {}) if "read_qc" in available else {}
    survey = outputs.get("genome_survey") if "genome_survey" in available else None
    # summary is the published current set; prefer its explicit post-filter form.
    if qc.get("post_filter_summary"):
        qc["summary"] = qc["post_filter_summary"]
    shared = state.get("shared_parameters") or {}
    if hasattr(shared, "model_dump"):
        shared = shared.model_dump(mode="json")
    facts = build_canonical_facts(
        shared_parameters=shared,
        read_qc_output=qc or None,
        genome_survey_result=survey,
        asset_flags={
            "has_hifi": bool(assembly.get("hifi_reads_ref")),
            "has_ont": bool(assembly.get("ont_reads_ref")),
            "has_hic": bool(assembly.get("has_hic") or assembly.get("hic_reads_ref")),
        },
    )
    observations = []
    for name, q in (
        ("genome_size", facts.genome.effective_genome_size),
        ("heterozygosity", facts.genome.heterozygosity),
        ("repeat_fraction", facts.genome.repeat_fraction),
        ("ploidy", facts.species.ploidy),
    ):
        if q:
            observations.append(
                Observation(
                    name=name,
                    value=q.value,
                    unit=q.unit,
                    object_id="sample",
                    basis=q.origin.kind,
                )
            )
    platforms: set[str] = set()
    for members in (assembly.get("assembly_input_member_platforms") or {}).values():
        platforms.update(PLATFORMS[p] for p in members if p in PLATFORMS)
    for key, platform in (
        ("hifi_reads_ref", "hifi"),
        ("ont_reads_ref", "ont"),
        ("hic_reads_ref", "hic"),
    ):
        if assembly.get(key):
            platforms.add(platform)
    if assembly.get("has_hic"):
        platforms.add("hic")
    qc_platforms = set(qc.get("input_member_platforms") or [])
    filtered_paths = qc.get("filtered_long_reads_refs") or (
        [qc["filtered_long_reads_ref"]] if qc.get("filtered_long_reads_ref") else []
    )
    qc_paths = (
        filtered_paths
        if qc.get("post_filter_summary")
        else qc.get("raw_long_reads_paths")
    ) or []
    qc_state = "filtered" if qc.get("post_filter_summary") else "raw"
    coverage = facts.sequencing.raw_coverage
    if qc_state == "filtered":
        # The common schema names this field raw_coverage; filtered coverage lives
        # only in the explicitly state-bound material observations below.
        facts = facts.model_copy(
            update={
                "sequencing": facts.sequencing.model_copy(update={"raw_coverage": None})
            }
        )
    # An aggregate over several platforms remains unbound, even if one is HiFi.
    if len(qc_platforms) == 1 and qc_paths:
        qc_platform = PLATFORMS.get(next(iter(qc_platforms)))
        if qc_platform:
            for name, q in (
                ("total_bases", facts.reads.total_bases),
                ("read_n50", facts.reads.read_n50),
                ("mean_read_length", facts.reads.mean_read_length),
                ("coverage", coverage),
            ):
                if q:
                    observations.append(
                        Observation(
                            name=name,
                            value=q.value,
                            unit=q.unit,
                            object_id="qc_read_set",
                            platform=qc_platform,
                            state=qc_state,
                            origin="derived" if name == "coverage" else "reported",
                            basis="QC total bases / effective genome size"
                            if name == "coverage"
                            else "QC of identified read set",
                        )
                    )
    text = " ".join(
        str(v)
        for v in (
            facts.species.scientific_name,
            facts.species.clade,
        )
        if v
    )
    return {
        "task": "contig_assembly",
        "text": text,
        "platforms": sorted(platforms),
        "material_inputs": resolved_material_context(assembly).model_dump(mode="json"),
        "target_level": assembly.get("target_level"),
        "observations": [o.model_dump(mode="json") for o in observations],
        "read_summary_scope": {
            "platforms": sorted(qc_platforms),
            "state": qc_state,
            "bound": bool(qc_paths),
            "aggregate": len(qc_platforms) != 1,
        },
        "read_binding": {
            "paths": qc_paths,
            "platforms": sorted(qc_platforms),
            "state": qc_state,
        },
        "facts": facts.model_dump(mode="json", exclude_none=True),
    }


def visible_query(query: Mapping[str, Any]) -> dict[str, Any]:
    """Material scope only; canonical facts are sent once, separately."""
    result = {
        k: query[k]
        for k in ("platforms", "read_summary_scope", "material_inputs")
        if k in query
    }
    for observation in query.get("observations", []):
        if observation["name"] == "coverage" and observation["state"] == "filtered":
            result["filtered_coverage"] = {
                k: observation[k] for k in ("value", "unit", "platform", "basis")
            }
    return result
