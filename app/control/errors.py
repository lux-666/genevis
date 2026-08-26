"""Lifecycle error hierarchy.

Defined here so that both ``lifecycle.py`` and modules inside ``app.review``
can import these types without creating a circular dependency.
"""

from __future__ import annotations


class LifecycleError(ValueError):
    """Base error for lifecycle operations."""


class ResumeValidationError(LifecycleError):
    """Raised when a resume request cannot be applied."""


class UnsupportedLifecycleOperationError(LifecycleError):
    """Raised for lifecycle operations outside the current task scope."""
