"""Checkpointer factory."""

from __future__ import annotations


def get_checkpointer(backend: str = "memory"):
    """Return a LangGraph checkpointer.

    The scaffold supports memory checkpoints first. Durable sqlite/postgres
    checkpoints are planned after the graph contract stabilizes.
    """
    if backend != "memory":
        raise ValueError(f"Unsupported checkpointer backend in scaffold: {backend}")

    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()
