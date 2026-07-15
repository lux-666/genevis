"""Method-specific rendering for the unified parameter-patch prompt."""

from __future__ import annotations

import json
from typing import Any, Mapping

from app.llm.assembly_parameter_context import build_assembly_parameter_context
from app.llm.prompts import load_prompt_bundle, render_prompt_messages


_BUNDLE_BY_METHOD = {
    "flye": "assembly.parameter_flye",
    "nextdenovo": "assembly.parameter_nextdenovo",
    "hifiasm": "assembly.parameter_hifiasm",
    "hifiasm_ont": "assembly.parameter_hifiasm_ont",
}


def render_assembly_parameter_messages(
    prompt_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Render one instruction pair without response examples or duplicated cards."""

    model_context = build_assembly_parameter_context(prompt_payload)
    method_id = _normalized_method_id(model_context.get("method_id"))
    try:
        bundle_name = _BUNDLE_BY_METHOD[method_id]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported assembly parameter prompt method: {method_id!r}"
        ) from exc
    return render_prompt_messages(
        load_prompt_bundle(bundle_name),
        {
            "method_id": method_id,
            "prompt_payload_json": json.dumps(
                model_context,
                ensure_ascii=False,
                indent=2,
            ),
        },
    )


def parameter_prompt_bundle_name(method_id: Any) -> str:
    """Return the prompt bundle selected for an assembly method."""

    normalized = _normalized_method_id(method_id)
    try:
        return _BUNDLE_BY_METHOD[normalized]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported assembly parameter prompt method: {normalized!r}"
        ) from exc


def _normalized_method_id(method_id: Any) -> str:
    return str(method_id or "").strip().rsplit(".", 1)[-1]
