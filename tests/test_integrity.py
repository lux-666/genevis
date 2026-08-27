"""Tests for shared exact-byte integrity primitives."""

from __future__ import annotations

import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from app.integrity import _sha256_file_for_fingerprint, sha256_file


class Sha256FileTests(unittest.TestCase):
    def setUp(self) -> None:
        _sha256_file_for_fingerprint.cache_clear()

    def tearDown(self) -> None:
        _sha256_file_for_fingerprint.cache_clear()

    def test_reuses_digest_until_file_stat_fingerprint_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "reads.fastq"
            source.write_bytes(b"alpha")

            self.assertEqual(sha256_file(source), sha256(b"alpha").hexdigest())
            self.assertEqual(sha256_file(source), sha256(b"alpha").hexdigest())
            self.assertEqual(_sha256_file_for_fingerprint.cache_info().hits, 1)

            before = source.stat()
            source.write_bytes(b"bravo")
            os.utime(
                source,
                ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
            )

            self.assertEqual(sha256_file(source), sha256(b"bravo").hexdigest())
            self.assertEqual(_sha256_file_for_fingerprint.cache_info().misses, 2)


if __name__ == "__main__":
    unittest.main()
