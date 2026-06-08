"""Tests for parent graph routing boundaries."""

from __future__ import annotations

import unittest

from app.graph.routing import route_controller


class GraphRoutingTests(unittest.TestCase):
    def test_requested_stages_skip_downstream_after_assembly_goal(self):
        update = route_controller(
            {
                "supervision_state": "idle",
                "current_stage": "assembly_review",
                "requested_stages": ["read_qc", "assembly"],
            }
        )

        self.assertEqual(update["current_stage"], "done")


if __name__ == "__main__":
    unittest.main()
