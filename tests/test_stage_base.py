"""Tests for bounded shared stage-state helpers."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.schemas.state import TopLevelState, initial_state
from app.stages.base import append_route_trace, session_paths
from app.storage.paths import DataLayoutError
from tests.data_v2 import v2_run_dir


class RouteTraceTests(unittest.TestCase):
    def test_append_route_trace_retains_only_latest_sixteen_items(self):
        original = [f"old-{index}" for index in range(20)]
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
        )
        state["route_trace"] = original

        trace = append_route_trace(state, "new-a", "new-b")

        self.assertEqual(
            trace,
            [
                *original[6:],
                "step-000-inventory:new-a",
                "step-000-inventory:new-b",
            ],
        )
        self.assertEqual(len(trace), ROUTE_TRACE_LIMIT)
        self.assertEqual(state["route_trace"], original)


class StageSessionPathTests(unittest.TestCase):
    def test_state_session_id_must_match_the_run_root_identity(self):
        with TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(Path(tmp) / "data", "owned-session")
            state = cast(
                TopLevelState,
                {"session_id": "different-session", "data_dir": str(run_dir)},
            )

            with self.assertRaises(DataLayoutError):
                session_paths(state)


if __name__ == "__main__":
    unittest.main()
