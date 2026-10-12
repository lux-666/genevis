"""Crash recovery preserves the published response across a stale checkpoint."""

import pytest

from app.llm.transaction import recover_or_invoke_response


def test_recovery_does_not_invoke_or_republish():
    record = {"exact": "response"}
    ref = object()

    def forbidden():
        raise AssertionError("must recover committed response")

    actual_ref, result = recover_or_invoke_response(
        recover=lambda: (ref, record),
        invoke=forbidden,
        restore=lambda payload: payload["exact"],
        publish=lambda _: forbidden(),
    )
    assert actual_ref is ref
    assert result == "response"


def test_published_response_survives_publication_acknowledgment_loss():
    committed = []
    calls = []
    ref = object()

    def invoke():
        calls.append(1)
        return {"exact": "response"}

    def publish(decision):
        committed.append((ref, decision))
        raise OSError("lost acknowledgment after durable publication")

    with pytest.raises(OSError):
        recover_or_invoke_response(
            recover=lambda: None, invoke=invoke, restore=dict, publish=publish
        )
    actual_ref, result = recover_or_invoke_response(
        recover=lambda: committed[0], invoke=invoke, restore=dict, publish=publish
    )
    assert actual_ref is ref
    assert result == {"exact": "response"}
    assert calls == [1]
