"""Test suite package init.

Sets ``GENE_AGENT_CHECKPOINTER=memory`` by default so unit tests do not
spill sqlite checkpoint files into temporary directories or the cwd.
Tests that exercise the durable resume path (``test_durable_resume``,
``test_checkpointer``) override this explicitly.

The supported full-suite entry point is pytest via scripts/server_test.sh.
For focused unittest-only checks, use discovery with ``-t .`` so this package
is imported; unittest discovery does not collect the pytest function tests.
"""

from __future__ import annotations

import os

os.environ.setdefault("GENE_AGENT_CHECKPOINTER", "memory")
