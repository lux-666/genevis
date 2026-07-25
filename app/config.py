"""Process-level configuration defaults independent of runtime layers."""

from __future__ import annotations

import os

DEFAULT_DATA_ROOT = "data"


def default_data_root() -> str:
    """Return the configured root for newly created workflow sessions."""

    return os.environ.get("GENE_AGENT_DATA_ROOT", DEFAULT_DATA_ROOT)
