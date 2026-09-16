"""Material and lexical retrieval with source diversity, not method filtering."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Literal

from app.domains.assembly.software_knowledge import explain_settings
from app.knowledge.assembly_cases import (
    KnowledgeSnapshot,
    Observation,
    canonical_tool,
    json_size,
)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_-]+|[\u4e00-\u9fff]", text.casefold()))


def _matches(
    query: list[Observation], observed: tuple[Observation, ...]
) -> dict[str, float]:
    result = {}
    for q in query:
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
        result[f"{q.object_id}:{q.name}"] = (
            float(q.value == v)
            if q.name == "ploidy"
            else max(0.0, 1 - abs(q.value - v) / max(abs(q.value), abs(v), 1e-12))
        )
    return result


def retrieve_assembly_cases(
    snapshot: KnowledgeSnapshot,
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
    target = snapshot.software.get(selected_method or "", {}).get("tool", "")
    rows: list[dict[str, Any]] = []
    for paper in snapshot.papers:
        for case in paper.cases:
            matches = _matches(observations, case.observations)
            material = (
                sum(matches.values()) / len(observations) if observations else 0.0
            )
            tokens = _tokens(str(case.content) + " " + str(case.method))
            lexical = len(words & tokens) / math.sqrt(max(1, len(words) * len(tokens)))
            direct = bool(
                target and canonical_tool(case.method.tool) == canonical_tool(target)
            )
            # Same-tool status alone is insufficient evidence of relevance.
            if not matches and not words & tokens:
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
        "software_explanations": [],
        "limitations": [
            "Cases and software knowledge inform a useful first recommendation; "
            "retrieval scores are not performance probabilities."
        ],
    }
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
                "case_id": case.case_id,
                "paper": paper.paper,
                "evidence_scope": paper.evidence_scope,
                "method": case.method.model_dump(mode="json"),
                "execution_status": case.execution_status,
                "route_roles": list(case.route_roles),
                "input_platforms": list(case.input_platforms),
                "content": case.content,
                "match": {
                    "relation": "same_tool" if row["direct"] else "cross_method",
                    "observations": row["matches"],
                    "missing": [
                        f"{q.object_id}:{q.name}"
                        for q in observations
                        if f"{q.object_id}:{q.name}" not in row["matches"]
                    ],
                    "platform_differences": sorted(
                        set(case.input_platforms) ^ set(query.get("platforms", []))
                    ),
                },
            }
            explanation = explain_settings(case.method, snapshot.software)
            explanations = list(result["software_explanations"])
            if explanation not in explanations:
                explanations.append(explanation)
            tentative = {
                **result,
                "cases": [*result["cases"], card],
                "software_explanations": explanations,
            }
            if json_size(tentative) > max_chars:
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
