"""Helpers for reading dotted paths from assembly profile mappings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def lookup_mapping_path(values: Any, path: str) -> Any:
    """Return a dotted-path value, or ``None`` when the path is absent."""

    current = values
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current
