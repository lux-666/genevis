"""Method-specific rendering for the unified parameter-patch prompt."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Mapping

from app.llm.prompts import load_prompt_bundle, render_prompt_messages


_BUNDLE_BY_METHOD = {
    "flye": "assembly.parameter_flye",
    "nextdenovo": "assembly.parameter_nextdenovo",
    "hifiasm": "assembly.parameter_hifiasm",
    "hifiasm_ont": "assembly.parameter_hifiasm_ont",
}


def render_assembly_parameter_messages(
    request_dto: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Render the strict parameter DTO into one provider-neutral message pair."""

    method_id = _normalized_method_id(request_dto.get("method_id"))
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
            "parameter_request_json": json.dumps(
                dict(request_dto),
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


def parameter_prompt_bundle_hash(method_id: Any) -> str:
    """Hash the exact immutable prompt templates selected for a method."""

    bundle = load_prompt_bundle(parameter_prompt_bundle_name(method_id))
    canonical = json.dumps(
        {
            "name": bundle.name,
            "system": bundle.system,
            "user_template": bundle.user_template,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _normalized_method_id(method_id: Any) -> str:
    return str(method_id or "").strip().rsplit(".", 1)[-1]
