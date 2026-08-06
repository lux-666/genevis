from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.domains.read_inputs import (
    AssemblyInputError,
    resolve_assembly_read_paths,
)
from app.stages.assembly.method_application.nextdenovo import _fofn


class AssemblyFofnResolutionTests(unittest.TestCase):
    def test_relative_entries_resolve_against_fofn_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fofn_dir = root / "inputs"
            fofn_dir.mkdir()
            first = fofn_dir / "reads" / "first.fastq"
            second = root / "second.fastq"
            first.parent.mkdir()
            first.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            second.write_text("@r2\nACGT\n+\n!!!!\n", encoding="utf-8")
            fofn = fofn_dir / "reads.fofn"
            fofn.write_text(
                "reads/first.fastq\n\n" + str(second) + "\n", encoding="utf-8"
            )

            paths = resolve_assembly_read_paths(fofn, "long_reads.fofn")

            self.assertEqual(paths, [first.resolve(), second])

    def test_empty_or_missing_fofn_entries_use_canonical_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            empty = root / "empty.fofn"
            empty.write_text("\n", encoding="utf-8")
            with self.assertRaisesRegex(
                AssemblyInputError, "requires at least one read path"
            ):
                resolve_assembly_read_paths(empty, "long_reads.fofn")

            missing = root / "missing.fofn"
            missing.write_text("reads.fastq\n", encoding="utf-8")
            with self.assertRaisesRegex(AssemblyInputError, "read files not found"):
                resolve_assembly_read_paths(missing, "long_reads.fofn")

    def test_nextdenovo_materializes_resolved_paths_in_attempt_fofn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fofn_dir = root / "source"
            fofn_dir.mkdir()
            reads = fofn_dir / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            source_fofn = fofn_dir / "reads.fofn"
            source_fofn.write_text("reads.fastq\n", encoding="utf-8")
            run_dir = root / "data" / "runs" / "session"

            materialized, paths, error = _fofn(
                {
                    "session_id": "session",
                    "data_dir": str(run_dir),
                    "current_stage": "assembly",
                    "workflow_steps": [
                        {
                            "step_id": "step-000-inventory",
                            "stage": "inventory",
                            "allow_local_execution": False,
                        },
                        {
                            "step_id": "step-001-assembly",
                            "stage": "assembly",
                            "allow_local_execution": False,
                        },
                    ],
                    "workflow_step_index": 1,
                    "entered_workflow_step_index": 1,
                },
                source_fofn,
                "long_reads.fofn",
                attempt_id=1,
            )

            self.assertIsNone(error)
            self.assertEqual(paths, [reads.resolve()])
            self.assertIsNotNone(materialized)
            self.assertEqual(
                materialized.read_text(encoding="utf-8"), f"{reads.resolve()}\n"
            )


if __name__ == "__main__":
    unittest.main()
