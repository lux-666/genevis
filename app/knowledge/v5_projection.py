"""Explicit v5 graph selections to cases; no inferred stages or graph edges."""

from __future__ import annotations

import re
from typing import Any

from app.knowledge.assembly_cases import (
    AssemblyCase,
    CasePaper,
    Observation,
    SourceMethod,
)

_UNITS = {
    "bp": ("bp", 1),
    "kb": ("bp", 1000),
    "Mb": ("bp", 1e6),
    "Gb": ("bp", 1e9),
    "%": ("fraction", 0.01),
    "x": ("x", 1),
    "×": ("x", 1),
}


def _number(raw: str, unit: str) -> float | None:
    match = re.fullmatch(
        r"\s*(?:~|approximately\s+)?([\d,]+(?:\.\d+)?)\s*(bp|kb|Mb|Gb|%|x|×)\s*", raw
    )
    if not match or _UNITS[match[2]][0] != unit:
        return None
    value = float(match[1].replace(",", "")) * _UNITS[match[2]][1]
    if unit == "fraction" and not 0 <= value <= 1:
        return None
    return value


def _feature_name(key: str) -> tuple[str, str | None]:
    platform = next(
        (p for p in ("hifi", "ont", "clr") if key.startswith(p + "_")), None
    )
    name = key[len(platform) + 1 :] if platform else key
    return name.removesuffix("_bp"), platform


def project_paper(
    paper_id: str, graph: dict[str, Any], selections: list[dict[str, Any]]
) -> CasePaper:
    if graph.get("schema_version") != "assembly-extraction.v5":
        raise ValueError("only assembly-extraction.v5 graphs are accepted")
    groups = (
        "sources",
        "samples",
        "states",
        "methods",
        "steps",
        "routes",
        "measurements",
        "gaps",
    )
    indexes = {k: {item["id"]: item for item in graph[k]} for k in groups}
    ids = [item["id"] for k in groups for item in graph[k]]
    if len(ids) != len(set(ids)):
        raise ValueError("v5 graph contains duplicate entity IDs")
    states, metrics = indexes["states"], indexes["measurements"]
    cases = []
    for binding in selections:
        if binding["paper"] != paper_id or binding["stage"] != "assembly":
            continue
        if binding["task"] != "contig_assembly":
            raise ValueError(
                "only explicitly selected contig assembly tasks are supported"
            )
        step = indexes["steps"][binding["step"]]
        method = indexes["methods"][step["method_id"]]
        direct = {i["state_id"] for i in step["inputs"]}
        if not set(binding["inputs"]).issubset(direct):
            raise ValueError("input platform selection is not bound to this operation")
        sample_ids = set(step["sample_ids"])
        observations, original_measurements = [], []
        indexed: dict[str, Observation] = {}
        for key, spec in binding.get("features", {}).items():
            measurement = metrics[spec["measurement"]]
            original_measurements.append(measurement)
            bound = [states[s] for s in measurement["state_ids"]]
            name, platform = _feature_name(key)
            value = _number(measurement["value"], spec["unit"])
            # Do not scalarize shared/combined measurements or borrow upstream reads.
            if (
                spec.get("exclude_reason")
                or value is None
                or len(bound) != 1
                or measurement["basis"] != "each"
            ):
                continue
            material = bound[0]
            if (
                set(material["sample_ids"]) != sample_ids
                or material.get("sample_scope") != "individual"
            ):
                raise ValueError("measurement is not bound to this sample")
            if platform:
                if (
                    material["id"] not in direct
                    or binding["inputs"].get(material["id"]) != platform
                ):
                    raise ValueError(
                        "read observation is not bound to selected input/platform"
                    )
            elif (
                material["kind"] != "biological_genome"
                or material.get("origin") != "study"
            ):
                raise ValueError(
                    "sample features cannot be inferred from final assemblies "
                    "or settings"
                )
            observation = Observation(
                name=name,
                value=value,
                unit=spec["unit"],
                object_id=material["id"],
                platform=platform,
                state=binding.get("input_states", {}).get(material["id"], "unknown"),
                basis=f"{measurement['id']}: {measurement['value']}",
            )
            indexed[key] = observation
            observations.append(observation)
        for key, operands in binding.get("derived", {}).items():
            numerator, denominator = (indexed.get(k) for k in operands)
            name, platform = _feature_name(key)
            if numerator and denominator and denominator.value > 0:
                if (
                    name != "coverage"
                    or numerator.name != "total_bases"
                    or denominator.name != "genome_size"
                ):
                    raise ValueError(
                        "unsupported derived feature; no arbitrary numeric mapping"
                    )
                observations.append(
                    Observation(
                        name=name,
                        platform=platform,
                        value=numerator.value / denominator.value,
                        unit="x",
                        object_id=numerator.object_id,
                        state=numerator.state,
                        origin="derived",
                        basis=f"{numerator.basis} / {denominator.basis}",
                    )
                )
        # Only explicitly selected outcomes, preserving whole measurement groups.
        outcomes = [metrics[mid] for mid in binding.get("outcomes", [])]
        descendants = set(step["output_state_ids"])
        while True:
            expanded = descendants | {
                sid
                for operation in graph["steps"]
                if any(i["state_id"] in descendants for i in operation["inputs"])
                for sid in operation["output_state_ids"]
            }
            if expanded == descendants:
                break
            descendants = expanded
        if any(not descendants.intersection(m["state_ids"]) for m in outcomes):
            raise ValueError(
                "outcome is not attached to this operation or its descendants"
            )
        assessments = {
            m["assessment_step_id"]
            for m in original_measurements + outcomes
            if m.get("assessment_step_id")
        }
        routes = [r for r in graph["routes"] if step["id"] in r["step_ids"]]
        related = (
            direct
            | set(step["output_state_ids"])
            | {step["id"], method["id"]}
            | sample_ids
            | {m["id"] for m in original_measurements + outcomes}
            | {r["id"] for r in routes}
        )
        content = {
            "action": step["name"],
            "condition": step.get("condition"),
            "method_label": method.get("name"),
            "samples": [indexes["samples"][sid] for sid in step["sample_ids"]],
            "inputs": [
                {**i, "material": states[i["state_id"]]} for i in step["inputs"]
            ],
            "outputs": [states[sid] for sid in step["output_state_ids"]],
            "reported_measurements": original_measurements,
            "measurement_materials": [
                states[sid]
                for sid in sorted(
                    {sid for m in original_measurements for sid in m["state_ids"]}
                )
            ],
            "assessments": [
                {
                    **indexes["steps"][sid],
                    "method": indexes["methods"].get(
                        indexes["steps"][sid].get("method_id")
                    ),
                }
                for sid in sorted(assessments)
            ],
            "normalization_notes": {
                k: v["exclude_reason"]
                for k, v in binding.get("features", {}).items()
                if v.get("exclude_reason")
            },
            "sample_context": binding.get("ploidy_context"),
            "outcomes": [
                {**m, "measured_materials": [states[s] for s in m["state_ids"]]}
                for m in outcomes
            ],
            "routes": [
                {k: r[k] for k in ("name", "role", "selection_reason") if k in r}
                for r in routes
            ],
            "gaps": [
                g
                for g in graph["gaps"]
                if not g.get("affected_ids") or related & set(g["affected_ids"])
            ],
            "notes": binding.get("notes", []),
            "method_evidence": method.get("evidence", []),
            "step_evidence": step.get("evidence", []),
            "sources": [
                {
                    k: s[k]
                    for k in ("id", "name", "read_scope", "access", "limitation")
                    if k in s
                }
                for s in graph["sources"]
            ],
        }
        cases.append(
            AssemblyCase(
                case_id=f"{paper_id}:{step['id']}",
                configuration_id=f"{paper_id}:{method['id']}",
                method=SourceMethod(
                    tool=method["tool"],
                    version=method.get("version"),
                    settings=tuple(method.get("settings", [])),
                ),
                execution_status=step["execution_status"],
                route_roles=tuple(sorted({r["role"] for r in routes})),
                input_platforms=tuple(sorted(set(binding["inputs"].values()))),
                observations=tuple(observations),
                content=content,
            )
        )
    return CasePaper(
        schema_version="recommendation-cases.v1",
        paper_id=paper_id,
        paper=graph["paper"],
        cases=tuple(cases),
        evidence_scope=(
            "Explicit projection of existing v5 extraction; "
            "not a new full-paper audit or approval."
        ),
    )
