"""Explicit v5 graph selections to cases; no inferred stages or graph edges."""

from __future__ import annotations

import math
import re
from typing import Any, cast

from app.knowledge.assembly_cases import (
    AssemblyCase,
    CaseContent,
    CasePaper,
    Observation,
    SourceMethod,
)

_UNITS = {
    alias: (dimension, scale)
    for dimension, scale, aliases in (
        ("bp", 1, ("bp", "b", "base", "bases", "base pairs")),
        ("bp", 1e3, ("kb", "kbp", "kilobase", "kilobases")),
        ("bp", 1e6, ("mb", "mbp", "megabase", "megabases")),
        ("bp", 1e9, ("gb", "gbp", "gigabase", "gigabases")),
        ("bp", 1e12, ("tb", "tbp", "terabase", "terabases")),
        ("fraction", 0.01, ("%", "percent")),
        ("fraction", 1, ("fraction",)),
        ("x", 1, ("x", "×", "fold", "-fold", "times")),
        ("count", 1, ("count",)),
    )
    for alias in aliases
}
_SCALAR = re.compile(
    r"\s*(?:[~∼≈]|approximately\s+|roughly\s+|about\s+|around\s+|approx\.?\s+)?"
    r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*([a-z%× -]+)?\s*",
    re.IGNORECASE,
)


def _number(raw: str, unit: str, source_unit: str | None = None) -> float | None:
    """Parse a scalar, never a bound/range or a multi-metric sentence.

    A unit supplied by an explicit selection represents a source table header or
    metric label, not a guess from the desired output unit. Parentheses may only
    repeat the same quantity at a different precision/unit.
    """
    repeated = re.fullmatch(r"([^()]+)\s*\(([^()]+)\)\s*", raw)
    parts = list(repeated.groups()) if repeated else [raw]
    parsed = []
    for part in parts:
        match = _SCALAR.fullmatch(part)
        if not match:
            return None
        label = match[2].strip().casefold() if match[2] else source_unit
        label = label or ("count" if unit == "count" else None)
        definition = _UNITS.get(label.casefold()) if label else None
        if definition is None or definition[0] != unit:
            return None
        scalar = match[1].replace(",", "")
        value = float(scalar) * definition[1]
        if not math.isfinite(value) or (unit == "fraction" and not 0 <= value <= 1):
            return None
        decimals = len(scalar.partition(".")[2])
        parsed.append((value, definition[1] * 10**-decimals))
    if len(parsed) == 2 and abs(parsed[0][0] - parsed[1][0]) > sum(
        precision / 2 for _, precision in parsed
    ):
        return None
    return min(parsed, key=lambda item: item[1])[0]


def _feature_specs(binding: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Retain repeated/conflicting observations instead of choosing one value."""
    return [
        (key, spec)
        for key, specs in binding.get("features", {}).items()
        for spec in (specs if isinstance(specs, list) else [specs])
    ]


def _feature_name(key: str) -> tuple[str, str | None]:
    platform = next(
        (
            p
            for p in ("hifi", "ont", "clr", "hic", "short", "cyclone")
            if key.startswith(p + "_")
        ),
        None,
    )
    name = key[len(platform) + 1 :] if platform else key
    return name.removesuffix("_bp"), platform


def _selected_number(
    measurement: dict[str, Any], spec: dict[str, Any], name: str
) -> float | None:
    """Read an explicitly selected whole clause; never search for a number.

    Commas inside numbers are not separators. Keeping the whole clause prevents
    selecting just the scalar inside a bound, interval or subset description.
    Object, sample and processing-scope checks still apply to every component.
    """
    raw = measurement["value"]
    if component := spec.get("component"):
        repeated = re.fullmatch(r"([^()]+)\s*\(([^()]+)\)\s*", raw)
        if (
            repeated
            and all(
                _number(part, spec["unit"], spec.get("source_unit")) is not None
                for part in repeated.groups()
            )
            and _number(raw, spec["unit"], spec.get("source_unit")) is None
        ):
            return None
        clauses = [s.strip() for s in re.split(r"[;；()（）]|,(?!\d)", raw)]
        text = component["text"]
        if not text or clauses.count(text) != 1:
            raise ValueError("component must identify one complete source clause")
        label = component.get("label", "")
        allowed = {
            "read_n50": {"N50", "subread N50", "read N50"},
            "mean_read_length": {"mean read length", "mean length", "average"},
            "coverage": {"coverage"},
        }
        if label:
            if label not in allowed.get(name, set()) or not text.startswith(
                label + " "
            ):
                raise ValueError("component label does not match the selected feature")
            text = text[len(label) :].strip()
        raw = text
    return _number(raw, spec["unit"], spec.get("source_unit"))


def _material_label(state: dict[str, Any], samples: dict[str, Any]) -> str:
    subjects = ", ".join(samples[s].get("name", s) for s in state.get("sample_ids", []))
    return "; ".join(
        str(v)
        for v in (
            state.get("name", state["id"]),
            state.get("kind"),
            subjects,
            state.get("sample_scope"),
            state.get("origin"),
        )
        if v
    )


def _citations(entity: dict[str, Any], sources: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            f"{sources[e['source_id']]['name']}: {e['locator']}"
            for e in entity.get("evidence", [])
        )
    )


def _measurement_summary(
    measurement: dict[str, Any],
    indexes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    summary = {
        "metric": measurement["metric"],
        "value": measurement["value"],
        "objects": [
            _material_label(indexes["states"][s], indexes["samples"])
            for s in measurement["state_ids"]
        ],
        "basis": measurement["basis"],
        "context": measurement.get("context", []),
        "evidence": _citations(measurement, indexes["sources"]),
    }
    if measurement.get("assessment_step_id"):
        assessment = indexes["steps"][measurement["assessment_step_id"]]
        method = indexes["methods"].get(assessment.get("method_id"), {})
        summary["assessment"] = "; ".join(
            str(v)
            for v in (
                assessment["name"],
                method.get("tool"),
                method.get("version"),
                *method.get("settings", []),
            )
            if v
        )
    return {k: v for k, v in summary.items() if v}


def _outcome_attribution(
    state_ids: list[str], outputs: set[str], descendants: set[str]
) -> str:
    objects = set(state_ids)
    if not objects:
        return "unknown"
    if objects <= outputs:
        return "direct_output"
    if objects <= descendants - outputs:
        return "downstream_context"
    return "mixed_context"


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
        if not isinstance(binding.get("parameter_eligible"), bool):
            raise ValueError("each selected case needs parameter_eligible: bool")
        if binding["task"] != "contig_assembly":
            raise ValueError(
                "only explicitly selected contig assembly tasks are supported"
            )
        step = indexes["steps"][binding["step"]]
        method = indexes["methods"][step["method_id"]]
        direct = {i["state_id"] for i in step["inputs"]}
        if not set(binding["inputs"]).issubset(direct):
            raise ValueError("input platform selection is not bound to this operation")
        if not set(binding.get("input_states", {})).issubset(binding["inputs"]):
            raise ValueError("processing state is not bound to a selected read input")
        sample_ids = set(step["sample_ids"])
        observations = []
        original_measurements = {
            mid: metrics[mid] for mid in binding.get("reported_measurements", [])
        }
        material_ids = direct | {
            s["id"]
            for s in states.values()
            if s["kind"] == "biological_genome"
            and set(s["sample_ids"]) == sample_ids
            and s.get("origin") == "study"
        }
        if any(
            not material_ids.intersection(m["state_ids"])
            for m in original_measurements.values()
        ):
            raise ValueError("reported measurement is not attached to case materials")
        normalization_notes = {}
        indexed: dict[str, list[Observation]] = {}
        for key, spec in _feature_specs(binding):
            measurement = metrics[spec["measurement"]]
            if not material_ids.intersection(measurement["state_ids"]):
                raise ValueError(
                    "feature measurement is not attached to case materials"
                )
            original_measurements[measurement["id"]] = measurement
            bound = [states[s] for s in measurement["state_ids"]]
            name, platform = _feature_name(key)
            if spec.get("source_unit") and not spec.get("unit_basis"):
                raise ValueError(
                    "source unit selection requires an explicit unit basis"
                )
            value = _selected_number(measurement, spec, name)
            # Do not scalarize shared/combined measurements or borrow upstream reads.
            reason = spec.get("exclude_reason")
            if not reason and (len(bound) != 1 or measurement["basis"] != "each"):
                reason = (
                    "Shared/combined or unspecified measurement remains source text."
                )
            if not reason and value is None:
                reason = "No unambiguous scalar with a supported, explicit source unit."
            if reason or value is None:
                normalization_notes[f"{key}:{measurement['id']}"] = reason
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
                basis=f"{measurement['id']}: {measurement['value']}"
                + (f"; unit: {spec['unit_basis']}" if spec.get("source_unit") else "")
                + (
                    f"; selected component: {spec['component']['text']}"
                    if spec.get("component")
                    else ""
                ),
            )
            indexed.setdefault(key, []).append(observation)
            observations.append(observation)
        for key, operands in binding.get("derived", {}).items():
            candidates = [indexed.get(k, []) for k in operands]
            if len(candidates) != 2 or any(len(items) != 1 for items in candidates):
                normalization_notes[key] = (
                    "Derived feature requires two unique operands."
                )
                continue
            numerator, denominator = (items[0] for items in candidates)
            name, platform = _feature_name(key)
            if numerator and denominator and denominator.value > 0:
                if (
                    name != "coverage"
                    or numerator.name != "total_bases"
                    or denominator.name != "genome_size"
                    or numerator.platform != platform
                    or numerator.unit != "bp"
                    or denominator.unit != "bp"
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
                if any(
                    i["state_id"] in descendants and i["role"] == "target"
                    for i in operation["inputs"]
                )
                for sid in operation["output_state_ids"]
            }
            if expanded == descendants:
                break
            descendants = expanded
        if any(not descendants.intersection(m["state_ids"]) for m in outcomes):
            raise ValueError(
                "outcome is not attached to this operation or its descendants"
            )
        routes = [r for r in graph["routes"] if step["id"] in r["step_ids"]]
        related = (
            direct
            | set(step["output_state_ids"])
            | {step["id"], method["id"]}
            | sample_ids
            | set(original_measurements)
            | {m["id"] for m in outcomes}
            | {
                sid
                for m in [*original_measurements.values(), *outcomes]
                for sid in m["state_ids"]
            }
            | {r["id"] for r in routes}
        )
        content = {
            "action": step["name"],
            "condition": step.get("condition"),
            "method_label": method.get("name"),
            "samples": [
                {
                    "name": indexes["samples"][sid].get("name", sid),
                    "context": indexes["samples"][sid].get("context", []),
                }
                for sid in step["sample_ids"]
            ],
            "inputs": [
                {
                    "role": i["role"],
                    "material": _material_label(
                        states[i["state_id"]], indexes["samples"]
                    ),
                    **({"use": i["use"]} if i.get("use") else {}),
                }
                for i in step["inputs"]
            ],
            "outputs": [
                _material_label(states[sid], indexes["samples"])
                for sid in step["output_state_ids"]
            ],
            "reported_measurements": [
                _measurement_summary(m, indexes) for m in original_measurements.values()
            ],
            "normalization_notes": normalization_notes,
            "sample_context": binding.get("ploidy_context"),
            "outcomes": [
                {
                    **_measurement_summary(m, indexes),
                    "attribution": _outcome_attribution(
                        m["state_ids"], set(step["output_state_ids"]), descendants
                    ),
                }
                for m in outcomes
            ],
            "routes": [
                {k: r[k] for k in ("name", "role", "selection_reason") if r.get(k)}
                for r in routes
            ],
            "gaps": [
                {
                    "kind": g["kind"],
                    "issue": g["issue"],
                    "evidence": _citations(g, indexes["sources"]),
                    "leads": [
                        f"{indexes['sources'][e['source_id']]['name']}: {e['locator']}"
                        for e in g.get("leads", [])
                    ],
                }
                for g in graph["gaps"]
                if not g.get("affected_ids") or related & set(g["affected_ids"])
            ],
            "notes": binding.get("notes", []),
            "evidence": list(
                dict.fromkeys(
                    _citations(method, indexes["sources"])
                    + _citations(step, indexes["sources"])
                )
            ),
            "source_limits": [
                f"{s['name']}: {s['access']}; {s.get('limitation', '')}"
                for s in graph["sources"]
                if s.get("access") != "read" or s.get("limitation")
            ],
        }
        content = {k: v for k, v in content.items() if v}
        cases.append(
            AssemblyCase(
                case_id=f"{paper_id}:{step['id']}",
                configuration_id=f"{paper_id}:{method['id']}",
                parameter_eligible=binding["parameter_eligible"],
                method=SourceMethod(
                    tool=method["tool"],
                    version=method.get("version"),
                    settings=tuple(method.get("settings", [])),
                ),
                execution_status=step["execution_status"],
                route_roles=tuple(sorted({r["role"] for r in routes})),
                input_platforms=tuple(sorted(set(binding["inputs"].values()))),
                observations=tuple(observations),
                content=cast(CaseContent, content),
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
