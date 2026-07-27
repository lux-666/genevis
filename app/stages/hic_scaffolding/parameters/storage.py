"""AutoHiC parameter persistence — stage-bound delegation to shared evidence layer."""

from __future__ import annotations

from app.storage.parameter_evidence import ParameterPersistenceError, ParameterStageStorage

_storage = ParameterStageStorage(stage="hic_scaffolding")
write_artifact = _storage.write_artifact
read_artifact = _storage.read_artifact
recover_artifact = _storage.recover_artifact
publish_file = _storage.publish_file

__all__ = [
    "ParameterPersistenceError",
    "write_artifact",
    "read_artifact",
    "recover_artifact",
    "publish_file",
]
