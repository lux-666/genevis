"""Capture software explanations once; reference settings never become L2 keys."""

from __future__ import annotations

import re
from typing import Any

from app.domains.assembly.constants import ASSEMBLY_CANDIDATES
from app.domains.assembly.method_guidance import load_method_guidance
from app.domains.assembly.selection import METHOD_ALIASES, SHARED_TO_METHOD_READ_TYPE
from app.domains.parameter_recommendation.catalog import load_method_parameter_catalog
from app.knowledge.assembly_cases import SourceMethod, canonical_tool


def capture_software_knowledge() -> dict[str, Any]:
    records: dict[str, Any] = {}
    guidance = load_method_guidance()
    for method_id in ASSEMBLY_CANDIDATES:
        catalog = load_method_parameter_catalog(method_id)
        parameters = {}
        for key, spec in catalog.parameters.items():
            native = getattr(spec.application, "flag", None) or getattr(
                spec.application, "key", None
            )
            if native:
                parameters[native] = {
                    "name": key,
                    "meaning": spec.description or "Meaning not documented",
                    "notes": spec.notes,
                    "value_type": spec.value_type,
                    "adjustable_here": spec.layers.l2 is not None,
                }
        records[method_id] = {
            "tool": catalog.tool,
            "description": catalog.description,
            "supported_inputs": {
                read_type: {
                    alias: mode
                    for alias, mode in mappings.items()
                    if METHOD_ALIASES.get(alias) == method_id
                    and mode != "not_preferred"
                }
                for read_type, mappings in SHARED_TO_METHOD_READ_TYPE.items()
                if any(
                    METHOD_ALIASES.get(alias) == method_id and mode != "not_preferred"
                    for alias, mode in mappings.items()
                )
            },
            "parameters": parameters,
            "guidance": [
                r.model_card() for r in guidance.records if method_id in r.methods
            ],
        }
    return records


def explain_settings(method: SourceMethod, software: dict[str, Any]) -> dict[str, Any]:
    matches = [
        v
        for v in software.values()
        if canonical_tool(v["tool"]) == canonical_tool(method.tool)
    ]
    explanations: dict[str, Any] = {}
    for entry in matches:
        for native, spec in entry["parameters"].items():
            if any(
                re.search(r"(?<![\w-])" + re.escape(native) + r"(?![\w-])", s)
                for s in method.settings
            ):
                explanations[native] = {
                    "meaning": spec["meaning"],
                    "notes": spec["notes"],
                    "value_type": spec["value_type"],
                }
    return {
        "tool": method.tool,
        "reported_version": method.version,
        "settings": explanations,
        "scope": (
            "Consumer catalog semantics; historical version equivalence is not "
            "established. Unexplained source settings remain unknown."
        ),
    }


def candidate_descriptions(
    methods: list[str], software: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        {
            "method_id": m,
            "description": software[m]["description"],
            "supported_inputs": software[m]["supported_inputs"],
        }
        for m in methods
        if m in software
    ]
