"""Tests for CLI entrypoint behavior."""

from __future__ import annotations

import unittest

from app.entrypoints.run import _parse_args


class EntrypointTests(unittest.TestCase):
    def test_stages_flag_is_not_exposed_until_routing_semantics_exist(self):
        with self.assertRaises(SystemExit):
            _parse_args(
                [
                    "run",
                    "--session-id",
                    "s1",
                    "--manifest",
                    "manifest.yaml",
                    "--stages",
                    "inventory",
                ]
            )


if __name__ == "__main__":
    unittest.main()
