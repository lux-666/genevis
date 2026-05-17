"""Tests for review policy evaluation."""

from __future__ import annotations

import unittest

from app.review.base import ReviewPayload
from app.review.policy import evaluate


class ReviewPolicyTests(unittest.TestCase):
    def test_missing_policy_blocks_indefinitely(self):
        request = ReviewPayload(stage="assembly_review", reason="review")

        outcome = evaluate("assembly_review", request, None)

        self.assertEqual(outcome.action, "BLOCK_INDEFINITE")
        self.assertEqual(outcome.mode, "strict")

    def test_strict_policy_with_timeout_blocks_with_timeout(self):
        request = ReviewPayload(stage="assembly_review", reason="review")

        outcome = evaluate("assembly_review", request, {"mode": "strict", "max_blocked_seconds": 60})

        self.assertEqual(outcome.action, "BLOCK_WITH_TIMEOUT")
        self.assertEqual(outcome.max_blocked_seconds, 60)

    def test_auto_policy_auto_decidable_decides_now(self):
        request = ReviewPayload(stage="assembly_review", reason="review", auto_decidable=True)

        outcome = evaluate("assembly_review", request, {"mode": "auto"})

        self.assertEqual(outcome.action, "AUTO_DECIDE_NOW")
        self.assertTrue(outcome.auto_decidable)

    def test_auto_policy_non_auto_decidable_blocks_pending_confirm(self):
        request = ReviewPayload(stage="hic_scaffolding", reason="review", auto_decidable=False)

        outcome = evaluate("hic_scaffolding", request, {"mode": "auto"})

        self.assertEqual(outcome.action, "BLOCK_PENDING_CONFIRM")
        self.assertFalse(outcome.auto_decidable)

    def test_hic_decisions_are_valid_review_payload_decisions(self):
        request = ReviewPayload(
            stage="hic_scaffolding",
            reason="review",
            decisions_allowed=["select_method", "skip", "stop_human_review"],
            auto_decidable=False,
        )

        self.assertEqual(request.decisions_allowed, ["select_method", "skip", "stop_human_review"])


if __name__ == "__main__":
    unittest.main()
