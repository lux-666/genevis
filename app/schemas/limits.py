"""Shared size limits for bounded durable state and read models."""

ROUTE_TRACE_LIMIT = 16
"""Maximum route entries retained in checkpoints, ledger summaries, and reports."""

TOP_LEVEL_STATE_TARGET_BYTES = 64 * 1024
"""Canonical JSON target for the complete checkpointed ``TopLevelState``."""

TOP_LEVEL_STATE_HARD_MAX_BYTES = 256 * 1024
"""Hard canonical JSON ceiling for the complete checkpointed state."""

ACTIVE_INTERRUPT_MAX_BYTES = 4 * 1024
"""Hard canonical JSON ceiling for the active human-review cursor."""

LAST_ERROR_MAX_CHARS = 4 * 1024
"""Maximum inline terminal/blocking diagnostic length."""

REVIEW_NOTES_MAX_ITEMS = 16
"""Maximum number of inline operator review notes retained in state."""

REVIEW_NOTE_MAX_CHARS = 2 * 1024
"""Maximum length of one inline review note; longer notes are artifacts."""

STAGE_CURSOR_MAX_BYTES = 64 * 1024
"""Maximum canonical JSON size of one stage cursor."""

STAGE_OUTPUTS_MAX_BYTES = 256 * 1024
"""Hard ceiling for the complete checkpointed ``stage_outputs`` mapping."""

RESOURCE_REFS_MAX_BYTES = 128 * 1024
"""Hard ceiling for the checkpointed cross-stage resource reference table."""
