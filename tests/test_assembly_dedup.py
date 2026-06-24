"""Tests for the assembly_dedup stage."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.control.ledger import Ledger
from app.schemas.state import initial_state
from app.stages.assembly_dedup.builder import build_stage as build_dedup_stage
from app.stages.assembly_review.builder import build_stage as build_review_stage


class AssemblyDedupStageTests(unittest.TestCase):
    def _candidate_bundle(self, tmpdir: str, *, candidate_id: str, draft_ref: str) -> Path:
        path = Path(tmpdir) / f"{candidate_id.replace(':', '_')}.json"
        path.write_text(
            json.dumps(
                {
                    "session_id": "s1",
                    "candidate_id": candidate_id,
                    "method_name": "assembly.candidate.flye",
                    "status": "executed",
                    "artifact_type": "draft_assembly.candidate",
                    "input_ref": "reads.fofn",
                    "input_type": "long_reads.fofn",
                    "expected_outputs": {"draft_assembly_fasta": draft_ref},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def _state(self, tmpdir: str):
        state = initial_state(
            session_id="s1",
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
        )
        state["current_stage"] = "assembly_dedup"
        return state

    def test_passes_through_when_candidate_is_not_materialized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._candidate_bundle(
                tmpdir,
                candidate_id="s1:assembly.candidate.flye",
                draft_ref=str(Path(tmpdir) / "planned_only.fasta"),
            )
            state = self._state(tmpdir)
            state["stage_outputs"] = {
                "assembly": {"status": "planned", "selected_candidate_refs": [str(candidate_ref)]}
            }

            result = build_dedup_stage().invoke(state)

            dedup = result["stage_outputs"]["assembly_dedup"]
            self.assertEqual(result["current_stage"], "assembly_review")
            self.assertEqual(dedup["status"], "completed")
            self.assertEqual(dedup["skip_reason"], "no_materialized_draft_assembly")
            self.assertEqual(dedup["selected_candidate_refs"], [str(candidate_ref)])

    def test_passes_through_when_assembly_output_is_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)

            result = build_dedup_stage().invoke(state)

            dedup = result["stage_outputs"]["assembly_dedup"]
            self.assertEqual(result["current_stage"], "assembly_review", dedup.get("reason"))
            self.assertEqual(dedup["status"], "completed")
            self.assertEqual(dedup["skip_reason"], "no_materialized_draft_assembly")

    def test_assembly_review_preserves_raw_and_adds_dedup_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">contig1\nACGT\n", encoding="utf-8")
            purged = Path(tmpdir) / "purged.fasta"
            purged.write_text(">contig1\nACGT\n", encoding="utf-8")
            candidate_ref = self._candidate_bundle(
                tmpdir,
                candidate_id="s1:assembly.candidate.flye",
                draft_ref=str(draft),
            )
            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=tmpdir,
            )
            state["current_stage"] = "assembly_review"
            state["stage_outputs"] = {
                "assembly": {"status": "draft_ready", "selected_candidate_refs": [str(candidate_ref)]},
                "assembly_dedup": {
                    "status": "completed",
                    "candidate_ref": str(candidate_ref),
                    "candidate_id": "s1:assembly.candidate.flye",
                    "method_name": "assembly_dedup.purge_dups",
                    "input_assembly_ref": str(draft),
                    "purged_assembly_ref": str(purged),
                    "final_assembly_for_scaffolding_or_annotation": str(purged),
                },
            }

            result = build_review_stage().invoke(state)

            candidates = result["stage_outputs"]["assembly_review"]["candidates"]
            self.assertEqual([candidate["source"] for candidate in candidates], ["assembly_stage", "assembly_dedup_stage"])
            self.assertEqual(candidates[0]["final_assembly_ref"], str(draft))
            self.assertEqual(candidates[1]["final_assembly_ref"], str(purged))
            self.assertEqual(candidates[1]["pre_dedup_assembly_ref"], str(draft))

    def test_default_auto_purge_dups_writes_purged_candidate_without_overwriting_raw(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">contig1\nACGT\n", encoding="utf-8")
            candidate_ref = self._candidate_bundle(
                tmpdir,
                candidate_id="s1:assembly.candidate.flye",
                draft_ref=str(draft),
            )
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n####\n", encoding="utf-8")
            bin_dir = Path(tmpdir) / "bin"
            bin_dir.mkdir()
            minimap2 = bin_dir / "minimap2"
            minimap2.write_text("#!/bin/sh\nprintf 'alignment\\n'\n", encoding="utf-8")
            minimap2.chmod(0o755)
            samtools = bin_dir / "samtools"
            samtools.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = sort ]; then\n"
                "  shift\n"
                "  out=''\n"
                "  while [ \"$#\" -gt 0 ]; do\n"
                "    if [ \"$1\" = -o ]; then shift; out=\"$1\"; fi\n"
                "    shift\n"
                "  done\n"
                "  /bin/cat > /dev/null\n"
                "  : > \"$out\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = index ]; then : > \"$2.bai\"; fi\n",
                encoding="utf-8",
            )
            samtools.chmod(0o755)
            pbcstat = bin_dir / "pbcstat"
            pbcstat.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in *.paf.gz) ;; *) exit 9;; esac\n"
                "test -s \"$1\"\n"
                "printf 'stats\\n' > PB.stat\n"
                "printf 'cov\\n' > PB.base.cov\n",
                encoding="utf-8",
            )
            pbcstat.chmod(0o755)
            split_fa = bin_dir / "split_fa"
            split_fa.write_text("#!/bin/sh\n/bin/cat \"$1\"\n", encoding="utf-8")
            split_fa.chmod(0o755)
            calcuts = bin_dir / "calcuts"
            calcuts.write_text("#!/bin/sh\nprintf 'cutoffs\\n'\n", encoding="utf-8")
            calcuts.chmod(0o755)
            purge_dups = bin_dir / "purge_dups"
            purge_dups.write_text("#!/bin/sh\nprintf 'contig1\\t0\\t0\\n'\n", encoding="utf-8")
            purge_dups.chmod(0o755)
            get_seqs = bin_dir / "get_seqs"
            get_seqs.write_text(
                "#!/bin/sh\n"
                "prefix=''\n"
                "last=''\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = -p ]; then shift; prefix=\"$1\"; fi\n"
                "  last=\"$1\"\n"
                "  shift\n"
                "done\n"
                "/bin/cat \"$last\" > \"${prefix}.purged.fa\"\n"
                ": > \"${prefix}.hap.fa\"\n",
                encoding="utf-8",
            )
            get_seqs.chmod(0o755)

            state = self._state(tmpdir)
            state["stage_outputs"] = {
                "assembly": {
                    "status": "draft_ready",
                    "selected_candidate_refs": [str(candidate_ref)],
                    "input_ref": str(reads),
                    "primary_long_read_platform": "ont",
                },
                "assembly_dedup": {
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                },
            }

            result = build_dedup_stage().invoke(state)

            dedup = result["stage_outputs"]["assembly_dedup"]
            self.assertEqual(result["current_stage"], "assembly_review", dedup.get("reason"))
            self.assertEqual(dedup["status"], "completed")
            self.assertEqual(dedup["candidate_id"], "s1:assembly.candidate.flye")
            self.assertTrue(Path(dedup["purged_assembly_ref"]).exists())
            self.assertEqual(Path(dedup["purged_assembly_ref"]).read_text(encoding="utf-8"), draft.read_text(encoding="utf-8"))
            self.assertEqual(draft.read_text(encoding="utf-8"), ">contig1\nACGT\n")
            self.assertTrue(Path(dedup["purged_candidate_ref"]).exists())
            self.assertTrue((Path(tmpdir) / "artifacts" / "assembly_dedup" / "s1" / "reads_to_raw_assembly.paf.gz").is_file())
            self.assertTrue((Path(tmpdir) / "artifacts" / "assembly_dedup" / "s1" / "raw_assembly.split.fasta").is_file())
            self.assertEqual(dedup["cutoffs_strategy"], "calcuts_auto")
            self.assertTrue(any(ref.endswith("dups.bed") for ref in dedup["artifact_refs"]))
            event_types = [event["event_type"] for event in Ledger(tmpdir).read_events("s1")]
            self.assertIn("attempt_started", event_types)
            self.assertIn("attempt_succeeded", event_types)


if __name__ == "__main__":
    unittest.main()
