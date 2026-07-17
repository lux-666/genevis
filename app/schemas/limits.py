"""Shared size limits for bounded durable state and read models."""

ROUTE_TRACE_LIMIT = 16
"""Maximum route entries retained in checkpoints, ledger summaries, and reports."""

STAGE_CURSOR_MAX_BYTES = 64 * 1024
"""Maximum canonical JSON size of one stage cursor."""

STAGE_OUTPUTS_MAX_BYTES = 256 * 1024
"""Hard ceiling for the complete checkpointed ``stage_outputs`` mapping."""
