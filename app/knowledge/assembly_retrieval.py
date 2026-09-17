"""Material and lexical retrieval with source diversity, not method filtering."""

from __future__ import annotations

import math
import re
from collections import Counter
from copy import deepcopy
from typing import Any, Literal

from app.domains.assembly.software_knowledge import explain_settings
from app.knowledge.assembly_cases import (
    AssemblyKnowledge,
    Observation,
    canonical_tool,
    json_size,
)


def _measurement_groups(
    measurements: list[dict[str, Any]], sample_names: set[str]
) -> list[dict[str, Any]]:
    """Group by evaluated objects and basis; keep per-metric qualifications."""
    groups: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    for measurement in measurements:
        context: dict[str, Any] = {
            key: deepcopy(measurement[key])
            for key in ("objects", "basis", "attribution")
            if key in measurement
        }
        metric = {
            key: deepcopy(measurement[key])
            for key in ("metric", "value", "context", "assessment")
            if key in measurement
        }
        if context not in contexts:
            contexts.append(context)
            view = deepcopy(context)
            if "objects" in view:
                view["objects"] = [
                    "; ".join(
                        part
                        for i, part in enumerate(label.split("; "))
                        if i == 0 or len(sample_names) != 1 or part not in sample_names
                    )
                    for label in view["objects"]
                ]
            groups.append({**view, "metrics": []})
        metrics = groups[contexts.index(context)]["metrics"]
        if metric not in metrics:
            metrics.append(metric)
    for group in groups:
        metrics = group["metrics"]
        for key in ("context", "assessment"):
            if key in metrics[0] and all(
                key in m and m[key] == metrics[0][key] for m in metrics
            ):
                group[key] = metrics[0][key]
                for metric in metrics:
                    del metric[key]
    return groups


def project_method_choice_case(case: dict[str, Any]) -> dict[str, Any]:
    """Project an audit case for method choice; never forward its content blob."""
    content = case["content"]
    sample_names = {s["name"] for s in content.get("samples", [])}
    traits = [
        value
        for sample in content.get("samples", [])
        for value in [sample["name"], *sample.get("context", [])]
    ]
    if content.get("sample_context"):
        traits.append(content["sample_context"])
    reported = content.get("reported_measurements", [])
    observations = _measurement_groups(reported, sample_names)
    # Source groups can be unnormalizable. Index observations separately retain
    # their units/platform/state; the case contract does not guarantee a bijection.
    observations.extend(
        {
            key: deepcopy(o[key])
            for key in (
                "name",
                "value",
                "unit",
                "object_id",
                "platform",
                "state",
                "origin",
                "basis",
            )
            if key in o
        }
        for o in case["observations"]
    )
    roles = case["route_roles"]
    limitations = [
        case["evidence_scope"],
        *content.get("normalization_notes", {}).values(),
        *(
            "Extraction has explicit scope limits; unreported facts remain unknown."
            if g.get("kind") == "scope_limit"
            else g["issue"]
            for g in content.get("gaps", [])
            if g.get("issue")
        ),
        *content.get("notes", []),
        *content.get("source_limits", []),
    ]
    return {
        "case_id": case["case_id"],
        "method": {
            key: case["method"][key]
            for key in ("tool", "version", "mode")
            if case["method"].get(key) is not None
        },
        "operation": {
            key: deepcopy(content[key])
            for key in ("action", "condition", "method_label", "inputs", "outputs")
            if content.get(key)
        },
        "material": {
            "sample_traits": list(dict.fromkeys(traits)),
            "platforms": list(case["input_platforms"]),
            "observations": observations,
        },
        "execution_status": case["execution_status"],
        "route_role": roles[0] if len(roles) == 1 else list(roles),
        "outcomes": _measurement_groups(
            [{"attribution": "unknown", **m} for m in content.get("outcomes", [])],
            sample_names,
        ),
        "match": {
            key: deepcopy(case["match"].get(key, []))
            for key in (
                "matched_features",
                "differing_features",
                "comparisons",
                "missing",
                "platform_differences",
            )
        },
        "limitations": list(dict.fromkeys(v for v in limitations if v)),
    }


def project_parameter_case(
    case: dict[str, Any], *, target_method: str
) -> dict[str, Any]:
    """Keep source-native settings as context, never translate them to L2 keys."""
    if not target_method:
        raise ValueError("parameter projection requires a target tool")
    view = project_method_choice_case(case)
    view["source_method"] = {
        **view.pop("method"),
        "settings": list(case["method"]["settings"]),
        "setting_explanations": {
            "settings": {
                native: {
                    key: deepcopy(spec[key])
                    for key in ("meaning", "notes", "value_type")
                    if key in spec and spec[key] is not None
                }
                for native, spec in case["setting_explanations"]["settings"].items()
            },
            "scope": case["setting_explanations"]["scope"],
        },
    }
    view["target_method"] = target_method
    view["relation"] = (
        "same_tool"
        if canonical_tool(case["method"]["tool"]) == canonical_tool(target_method)
        else "cross_method"
    )
    return view


def project_assembly_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """The single prompt projection used by both request builders and budgeting."""
    purpose = evidence["purpose"]
    if purpose == "parameters":
        cases = [
            project_parameter_case(case, target_method=evidence["target_method"])
            for case in evidence["cases"]
        ]
    elif purpose == "method_choice":
        cases = [project_method_choice_case(case) for case in evidence["cases"]]
    else:
        raise ValueError("unknown evidence purpose")
    return {
        "purpose": purpose,
        "stage": evidence["stage"],
        "task": evidence["task"],
        "cases": cases,
        "limitations": list(evidence["limitations"]),
    }


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_-]+|[\u4e00-\u9fff]", text.casefold()))


def _matches(
    query: list[Observation], observed: tuple[Observation, ...]
) -> dict[int, dict[str, Any]]:
    result = {}
    for index, q in enumerate(query):
        values = [
            o.value
            for o in observed
            if (o.name, o.unit, o.platform, o.state)
            == (q.name, q.unit, q.platform, q.state)
        ]
        # Conflicting measurements do not become a best-of arbitrary choice.
        if len(set(values)) != 1:
            continue
        v = values[0]
        similarity = (
            float(q.value == v)
            if q.name == "ploidy"
            else max(0.0, 1 - abs(q.value - v) / max(abs(q.value), abs(v), 1e-12))
        )
        result[index] = {
            "name": q.name,
            "object_id": q.object_id,
            "similarity": similarity,
            "query_value": q.value,
            "case_value": v,
            "unit": q.unit,
            "platform": q.platform,
            "state": q.state,
        }
    return result


def retrieve_assembly_cases(
    knowledge: AssemblyKnowledge,
    query: dict[str, Any],
    *,
    purpose: Literal["method_choice", "parameters"],
    selected_method: str | None = None,
    limit: int = 5,
    max_chars: int = 32000,
) -> dict[str, Any]:
    if purpose not in {"method_choice", "parameters"}:
        raise ValueError("unknown retrieval purpose")
    if limit < 1 or max_chars < 256:
        raise ValueError("invalid evidence budget")
    observations = [
        Observation.model_validate(o) for o in query.get("observations", [])
    ]
    words = _tokens(query.get("text", ""))
    target = knowledge.software.get(selected_method or "", {}).get("tool", "")
    if purpose == "parameters" and not target:
        raise ValueError("parameter retrieval requires a known target method")
    rows: list[dict[str, Any]] = []
    for paper in knowledge.papers:
        for case in paper.cases:
            matches = _matches(observations, case.observations)
            material = (
                sum(m["similarity"] for m in matches.values()) / len(observations)
                if observations
                else 0.0
            )
            tokens = _tokens(str(case.content) + " " + str(case.method))
            lexical = len(words & tokens) / math.sqrt(max(1, len(words) * len(tokens)))
            direct = bool(
                target and canonical_tool(case.method.tool) == canonical_tool(target)
            )
            # Same-tool status alone is insufficient evidence of relevance.
            if material <= 0 and lexical <= 0:
                continue
            rows.append(
                {
                    "paper": paper,
                    "case": case,
                    "matches": matches,
                    "material": material,
                    "lexical": lexical,
                    "direct": direct,
                }
            )
    for row in rows:
        row["score"] = 0.0
    for metric in ("material", "lexical"):
        ranked = sorted(
            (r for r in rows if r[metric] > 0),
            key=lambda r: (-r[metric], r["case"].case_id),
        )
        # Equal component scores receive equal reciprocal ranks.
        previous, rank = None, 0
        for i, row in enumerate(ranked, 1):
            if row[metric] != previous:
                rank, previous = i, row[metric]
            row["score"] += (2 if metric == "material" else 1) / (10 + rank)
    result: dict[str, Any] = {
        "purpose": purpose,
        "stage": "assembly",
        "task": "contig_assembly",
        "cases": [],
        "limitations": [
            "Cases and software knowledge inform a useful first recommendation; "
            "retrieval scores are not performance probabilities."
        ],
    }
    if purpose == "parameters":
        result["target_method"] = target
        result["limitations"].append(
            "Cross-method settings are references only: do not directly map source "
            "parameter names or values to target parameters. Emit only the target "
            "adjustable_parameters keys; missing settings do not imply defaults."
        )
    trace: list[dict[str, Any]] = []
    groups: set[tuple[str, str]] = set()
    counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    remaining = list(rows)
    while remaining:
        # Diversity is a modest preference, not an equal software quota.
        row = max(
            remaining,
            key=lambda r: (
                r["score"]
                / (1 + 0.15 * tool_counts[canonical_tool(r["case"].method.tool)])
                + (0.002 if purpose == "parameters" and r["direct"] else 0),
                r["material"],
                r["case"].case_id,
            ),
        )
        remaining.remove(row)
        paper, case = row["paper"], row["case"]
        group = (paper.paper_id, case.configuration_id)
        source = paper.independence_group or paper.paper_id
        decision = "selected"
        if len(result["cases"]) >= limit:
            decision = "limit"
        elif group in groups or counts[source] >= 2:
            decision = "shared_configuration_or_source_limit"
        else:
            card = {
                **case.model_dump(mode="json"),
                "paper_id": paper.paper_id,
                "paper": dict(paper.paper),
                "evidence_scope": paper.evidence_scope,
                "independence_group": paper.independence_group,
                "setting_explanations": explain_settings(
                    case.method, knowledge.software
                ),
                "match": {
                    "matched_features": [
                        m["name"]
                        for m in row["matches"].values()
                        if m["similarity"] > 0
                    ],
                    "differing_features": [
                        m["name"]
                        for m in row["matches"].values()
                        if m["similarity"] < 1
                    ],
                    "comparisons": list(row["matches"].values()),
                    "missing": [
                        q.name
                        for index, q in enumerate(observations)
                        if index not in row["matches"]
                    ],
                    "platform_differences": sorted(
                        set(case.input_platforms) ^ set(query.get("platforms", []))
                    ),
                },
            }
            tentative = {
                **result,
                "cases": [*result["cases"], card],
            }
            if json_size(project_assembly_evidence(tentative)) > max_chars:
                decision = "whole_case_budget"
            else:
                result = tentative
                groups.add(group)
                counts[source] += 1
                tool_counts[canonical_tool(case.method.tool)] += 1
        trace.append(
            {
                "case_id": case.case_id,
                "material": row["material"],
                "lexical": row["lexical"],
                "score": row["score"],
                "decision": decision,
            }
        )
    return {"evidence": result, "trace": trace, "query": query}
