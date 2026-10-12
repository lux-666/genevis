"""A finishing phase cannot exist without its required evidence references."""

import pytest
from pydantic import ValidationError

from app.stages.finishing.state import FINISHING_CURSOR_ADAPTER


def _ref():
    return {
        "schema_version": 2,
        "artifact_id": "art_" + "a" * 32,
        "path_ref": "artifacts/example.json",
        "sha256": "a" * 64,
        "size_bytes": 1,
        "artifact_type": "test.evidence",
    }


@pytest.mark.parametrize(
    "phase, required",
    [
        ("advise", ("quast_ref", "reference_based")),
        ("blocked", ("quast_ref", "recommendation_ref")),
        ("run", ("quast_ref", "recommendation_ref", "decision_ref", "parameters")),
        ("done", ("decision_ref", "candidate_ref", "action")),
    ],
)
def test_phase_evidence_is_required(phase, required):
    cursor = {
        "phase": phase,
        "attempt": 0,
        "source_ref": _ref(),
        "config_ref": _ref(),
        "history_refs": [],
    }
    with pytest.raises(ValidationError):
        FINISHING_CURSOR_ADAPTER.validate_python(cursor)
    values = {"reference_based": False, "parameters": {}, "action": "keep_source"}
    cursor.update({key: values.get(key, _ref()) for key in required})
    assert FINISHING_CURSOR_ADAPTER.validate_python(cursor) == cursor
    for key in required:
        with pytest.raises(ValidationError):
            FINISHING_CURSOR_ADAPTER.validate_python(
                {k: v for k, v in cursor.items() if k != key}
            )


def test_unknown_phase_negative_attempt_and_unknown_fields_rejected():
    cursor = {
        "phase": "evaluate",
        "attempt": 0,
        "source_ref": _ref(),
        "config_ref": _ref(),
        "history_refs": [],
    }
    for change in ({"phase": "whatever"}, {"attempt": -1}, {"unowned": True}):
        with pytest.raises(ValidationError):
            FINISHING_CURSOR_ADAPTER.validate_python({**cursor, **change})
