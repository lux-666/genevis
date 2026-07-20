"""CWD-independent access to packaged runtime configuration."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from app.schemas.descriptors import DescriptorRegistry


def descriptors_root() -> Path:
    """Return the installed descriptor package root as a filesystem path."""

    packaged_root = Path(str(files("app._descriptors")))
    if (packaged_root / "registry.yaml").is_file():
        return packaged_root

    # Source checkouts keep descriptors at the repository root; resolve that
    # location from this module, never from process CWD.
    source_root = Path(__file__).resolve().parents[1] / "descriptors"
    if (source_root / "registry.yaml").is_file():
        return source_root
    raise FileNotFoundError("packaged descriptor registry is missing")


def descriptor_registry_path() -> Path:
    """Return the canonical descriptor registry path."""

    return descriptors_root() / "registry.yaml"


@lru_cache(maxsize=1)
def load_descriptor_registry() -> DescriptorRegistry:
    """Load and cache the canonical descriptor registry."""

    return DescriptorRegistry.load(descriptor_registry_path())
