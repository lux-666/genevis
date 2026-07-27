"""T2T parameter persistence — stage-bound delegation to shared evidence layer."""

from __future__ import annotations

from app.storage.parameter_evidence import ParameterPersistenceError, ParameterStageStorage

_storage = ParameterStageStorage(stage="t2t")
write_artifact = _storage.write_artifact
read_artifact = _storage.read_artifact
recover_artifact = _storage.recover_artifact

__all__ = [
    "ParameterPersistenceError",
    "write_artifact",
    "read_artifact",
    "recover_artifact",
]
