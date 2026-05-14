"""Test suite package init.

Sets ``GENE_AGENT_CHECKPOINTER=memory`` by default so unit tests do not
spill sqlite checkpoint files into temporary directories or the cwd.
Tests that exercise the durable resume path (``test_durable_resume``,
``test_checkpointer``) override this explicitly.

For this init to be executed by ``python -m unittest discover``, the
discover invocation must use ``-t .`` so the runner imports ``tests``
as a regular package; ``-s tests`` alone treats it as a namespace
package and skips the init. All scripts and docs in this repo use
``-t .``.
"""

from __future__ import annotations

import os

os.environ.setdefault("GENE_AGENT_CHECKPOINTER", "memory")
