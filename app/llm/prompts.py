"""Prompt bundle loading and provider-neutral rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PROMPT_ROOT = Path(__file__).resolve().parent / "prompt_bundles" / "prompts"


@dataclass(frozen=True)
class PromptBundle:
    """System prompt and user prompt template for one LLM task."""

    name: str
    system: str
    user_template: str


def load_prompt_bundle(name: str) -> PromptBundle:
    """Load ``<domain>.<task>`` prompt files from the prompt bundle tree."""

    try:
        domain, task = name.split(".", 1)
    except ValueError as exc:
        raise ValueError(
            "Prompt bundle name must look like '<domain>.<task>'."
        ) from exc

    prompt_dir = PROMPT_ROOT / domain
    system_path = prompt_dir / f"{task}_system.md"
    user_path = prompt_dir / f"{task}_user.md"
    if not system_path.exists() or not user_path.exists():
        raise FileNotFoundError(f"Prompt bundle not found: {name}")

    return PromptBundle(
        name=name,
        system=system_path.read_text(encoding="utf-8").strip(),
        user_template=user_path.read_text(encoding="utf-8").strip(),
    )


def render_prompt_messages(
    bundle: PromptBundle, context: Mapping[str, Any]
) -> list[dict[str, str]]:
    """Render a prompt bundle into provider-neutral chat messages."""

    values = dict(context)
    values.setdefault(
        "defaults_json",
        json.dumps(
            context.get("defaults", {}), ensure_ascii=False, indent=2, sort_keys=True
        ),
    )
    return [
        {"role": "system", "content": bundle.system},
        {
            "role": "user",
            "content": bundle.user_template.format_map(_SafeFormat(values)),
        },
    ]


class _SafeFormat(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""
