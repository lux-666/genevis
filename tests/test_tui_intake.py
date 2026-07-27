"""Focused tests for the TUI's pre-freeze sample intake core."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.entrypoints.tui_intake import (
    TuiDraftConflictError,
    apply_asset_answer,
    confirm_no_hic_degradation,
    create_rerun_tui_session_draft,
    create_tui_session_draft,
    deterministic_workflow_intent,
    extract_path_spans,
    load_tui_session_draft,
    materialize_manifest_draft,
    next_intake_clarification,
    parse_asset_answer_deterministically,
    parse_asset_answer_with_fallback,
    parse_workflow_intent,
    save_tui_session_draft,
)
from app.schemas.manifest import InputAssetSpec, PairedReadSpec
from app.schemas.tui_intake import HiCPairedReadsFact, HiCReadPair


class TuiIntakeTests(unittest.TestCase):
    def _file(self, root: Path, name: str, content: str = "reads") -> Path:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_goal_without_paths_creates_ont_and_hic_requirements(self):
        intent = deterministic_workflow_intent("我想对 T2T-CHM13 做 T2T 级组装")
        draft = create_tui_session_draft(
            session_id="s1",
            data_dir="data",
            request_text=intent.goal_text,
            intent=intent,
        )

        self.assertEqual(draft.intake.requirements, ["ont_reads", "hic_reads"])
        self.assertIsNone(draft.intake.ont_reads)
        self.assertEqual(next_intake_clarification(draft).role, "ont_reads")

    def test_llm_cannot_weaken_deterministic_t2t_intent(self):
        request = "sample CHM13 做 T2T 组装"

        intent = parse_workflow_intent(
            request,
            llm_parser=lambda _request: {
                "goal_text": "ignored",
                "sample_id": "CHM13",
                "target_species": "invented-species",
                "target_assembly_level": "contig",
                "requested_stages": ["assembly"],
            },
        )

        self.assertEqual(intent.goal_text, request)
        self.assertEqual(intent.sample_id, "CHM13")
        self.assertIsNone(intent.target_species)
        self.assertEqual(intent.target_assembly_level, "chromosome")
        self.assertIn("hic_scaffolding", intent.requested_stages)
        self.assertIn("t2t", intent.requested_stages)

    def test_path_extraction_keeps_invalid_path_for_local_validation(self):
        spans = extract_path_spans("ONT: /tmp/missing.fastq.gz, /tmp/a-directory")

        self.assertEqual(
            [span.path for span in spans],
            ["/tmp/missing.fastq.gz", "/tmp/a-directory"],
        )

    def test_embedded_supported_suffix_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            disguised = self._file(root, "reads.fastq.bak")
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text="assembly",
                intent=deterministic_workflow_intent("assembly"),
            )

            spans = extract_path_spans(f"ONT: {disguised}")
            result = apply_asset_answer(
                draft,
                answer=f"ONT: {disguised}",
                focused_role="ont_reads",
            )

            self.assertEqual([span.path for span in spans], [str(disguised)])
            self.assertIsNone(result.draft.intake.ont_reads)
            self.assertTrue(
                any("unsupported read-file suffix" in issue for issue in result.issues)
            )

    def test_explicit_unpaired_hic_is_not_reclassified_as_focused_ont(self):
        parsed = parse_asset_answer_with_fallback(
            "Hi-C: /tmp/unpaired.fastq",
            focused_role="ont_reads",
            llm_parser=lambda _answer, _focused: [
                {"role": "ont_reads", "paths": ["/tmp/unpaired.fastq"]}
            ],
        )

        self.assertEqual(parsed.candidates, [])
        self.assertEqual(parsed.unresolved_paths, ["/tmp/unpaired.fastq"])
        self.assertFalse(parsed.llm_used)
        self.assertTrue(
            any("deterministic role evidence" in issue for issue in parsed.issues)
        )

    def test_unpaired_hic_after_ont_label_stays_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ont = self._file(root, "ont.fastq")
            hic = self._file(root, "hic.fastq")
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text="assembly",
                intent=deterministic_workflow_intent("assembly"),
            )

            result = apply_asset_answer(
                draft,
                answer=f"ONT: {ont} Hi-C: {hic}",
                focused_role="ont_reads",
            )

            self.assertEqual(
                result.draft.intake.ont_reads.paths,
                [str(ont.resolve())],
            )
            self.assertIn(str(hic), result.draft.intake.answer_history[-1].issues[0])
            self.assertIsNone(result.draft.intake.hic_reads)

    def test_one_answer_can_confirm_ont_and_explicit_hic_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ont_1 = self._file(root, "ont/a.fastq.gz")
            ont_2 = self._file(root, "ont/b.fastq.gz")
            hic_r1 = self._file(root, "hic/r1.fastq.gz")
            hic_r2 = self._file(root, "hic/r2.fastq.gz")
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text="T2T",
                intent=deterministic_workflow_intent("T2T"),
            )

            result = apply_asset_answer(
                draft,
                answer=(f"ONT: {ont_1} {ont_2}; Hi-C R1: {hic_r1}; R2: {hic_r2}"),
                focused_role="ont_reads",
            )

            self.assertEqual(result.issues, [])
            self.assertEqual(result.accepted_roles, ["ont_reads", "hic_reads"])
            self.assertEqual(
                result.draft.intake.ont_reads.paths,
                [str(ont_1.resolve()), str(ont_2.resolve())],
            )
            hic = result.draft.intake.hic_reads
            self.assertEqual(hic.kind, "hic_paired_reads")
            self.assertEqual(hic.pairs[0].r1, str(hic_r1.resolve()))
            self.assertEqual(hic.pairs[0].r2, str(hic_r2.resolve()))

            manifest = materialize_manifest_draft(result.draft)
            hic_asset = next(
                asset
                for asset in manifest.assets
                if asset.asset_type == "hic_reads_registered"
            )
            self.assertEqual(
                hic_asset.paths,
                [str(hic_r1.resolve()), str(hic_r2.resolve())],
            )
            self.assertEqual(hic_asset.paired_reads[0].r1, str(hic_r1.resolve()))

    def test_hic_pair_can_be_completed_across_two_explicit_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r1 = self._file(root, "r1.fastq")
            r2 = self._file(root, "r2.fastq")
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text="T2T",
                intent=deterministic_workflow_intent("T2T"),
            )
            first = apply_asset_answer(
                draft,
                answer=f"Hi-C R1: {r1}",
                focused_role="hic_reads",
            )
            self.assertIsNone(first.draft.intake.hic_reads)
            self.assertTrue(first.draft.intake.unresolved_candidates)

            second = apply_asset_answer(
                first.draft,
                answer=f"Hi-C R2: {r2}",
                focused_role="hic_reads",
            )
            self.assertEqual(second.issues, [])
            self.assertEqual(
                second.draft.intake.hic_reads.pairs[0].r1, str(r1.resolve())
            )
            self.assertEqual(
                second.draft.intake.hic_reads.pairs[0].r2, str(r2.resolve())
            )
            self.assertEqual(second.draft.intake.unresolved_candidates, [])

    def test_pending_hic_candidates_survive_another_incomplete_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_r1 = self._file(root, "old-r1.fastq")
            new_r1 = self._file(root, "new-r1.fastq")
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text="T2T",
                intent=deterministic_workflow_intent("T2T"),
            )
            first = apply_asset_answer(
                draft,
                answer=f"Hi-C R1: {old_r1}",
                focused_role="hic_reads",
            )

            second = apply_asset_answer(
                first.draft,
                answer=f"Hi-C R1: {new_r1}",
                focused_role="hic_reads",
            )

            pending_paths = {
                path
                for item in second.draft.intake.unresolved_candidates
                for path in item.candidate.paths
            }
            self.assertEqual(
                pending_paths,
                {str(old_r1.resolve()), str(new_r1.resolve())},
            )

    def test_same_canonical_hic_mates_remain_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reads = self._file(root, "reads.fastq")
            alias = root / "reads-alias.fastq"
            alias.symlink_to(reads)
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text="T2T",
                intent=deterministic_workflow_intent("T2T"),
            )
            first = apply_asset_answer(
                draft,
                answer=f"Hi-C R1: {reads}",
                focused_role="hic_reads",
            )

            second = apply_asset_answer(
                first.draft,
                answer=f"Hi-C R2: {alias}",
                focused_role="hic_reads",
            )

            self.assertIsNone(second.draft.intake.hic_reads)
            self.assertTrue(any("different paths" in issue for issue in second.issues))
            self.assertEqual(len(second.draft.intake.unresolved_candidates), 2)

    def test_confirmed_hic_replacement_replaces_instead_of_appending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_r1 = self._file(root, "old-r1.fastq")
            old_r2 = self._file(root, "old-r2.fastq")
            new_r1 = self._file(root, "new-r1.fastq")
            new_r2 = self._file(root, "new-r2.fastq")
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text="T2T",
                intent=deterministic_workflow_intent("T2T"),
            )
            confirmed = apply_asset_answer(
                draft,
                answer=f"Hi-C R1: {old_r1}; R2: {old_r2}",
                focused_role="hic_reads",
            ).draft

            pending = apply_asset_answer(
                confirmed,
                answer=f"Hi-C R1: {new_r1}; R2: {new_r2}",
                focused_role="hic_reads",
            )
            self.assertEqual(pending.accepted_roles, [])
            self.assertEqual(
                pending.draft.intake.hic_reads.pairs[0].r1,
                str(old_r1.resolve()),
            )
            self.assertTrue(
                any(
                    "would replace confirmed Hi-C pairs" == item.reason
                    for item in pending.unresolved_candidates
                )
            )

            replaced = apply_asset_answer(
                pending.draft,
                answer=f"Hi-C R1: {new_r1}; R2: {new_r2}",
                focused_role="hic_reads",
                replace_confirmed=True,
            )
            self.assertEqual(replaced.accepted_roles, ["hic_reads"])
            self.assertEqual(len(replaced.draft.intake.hic_reads.pairs), 1)
            self.assertEqual(
                replaced.draft.intake.hic_reads.pairs[0].r1,
                str(new_r1.resolve()),
            )
            self.assertEqual(
                replaced.draft.intake.hic_reads.pairs[0].r2,
                str(new_r2.resolve()),
            )
            self.assertEqual(replaced.draft.intake.unresolved_candidates, [])

    def test_filename_does_not_pair_hic_reads_without_labels(self):
        parsed = parse_asset_answer_deterministically(
            "/tmp/sample_R1.fastq /tmp/sample_R2.fastq",
            focused_role="hic_reads",
        )

        self.assertEqual(parsed.candidates, [])
        self.assertEqual(
            parsed.unresolved_paths,
            ["/tmp/sample_R1.fastq", "/tmp/sample_R2.fastq"],
        )

    def test_invalid_and_directory_paths_are_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "reads"
            directory.mkdir()
            unsupported = self._file(root, "unsupported.txt")
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text="assembly",
                intent=deterministic_workflow_intent("assembly"),
            )

            result = apply_asset_answer(
                draft,
                answer=f"ONT: {directory} {unsupported}",
                focused_role="ont_reads",
            )

            self.assertIsNone(result.draft.intake.ont_reads)
            self.assertTrue(any("regular file" in issue for issue in result.issues))
            self.assertTrue(
                any("unsupported read-file suffix" in issue for issue in result.issues)
            )

    def test_llm_hallucinated_path_is_ignored_and_failure_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reads = self._file(root, "reads.fastq")

            def hallucinating(_answer, _focused):
                return [{"role": "ont_reads", "paths": ["/not/in/answer.fastq"]}]

            parsed = parse_asset_answer_with_fallback(
                f"ONT: {reads}",
                focused_role="ont_reads",
                llm_parser=hallucinating,
            )
            self.assertFalse(parsed.llm_used)
            self.assertTrue(
                any("absent from the user answer" in issue for issue in parsed.issues)
            )

            fallback = parse_asset_answer_with_fallback(
                f"ONT: {reads}",
                focused_role="ont_reads",
                llm_parser=lambda _answer, _focused: (_ for _ in ()).throw(
                    RuntimeError("offline")
                ),
            )
            self.assertEqual(
                [candidate.role for candidate in fallback.candidates], ["ont_reads"]
            )
            self.assertFalse(fallback.llm_used)

    def test_no_hic_requires_explicit_degradation_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ont = self._file(root, "ont.fastq")
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text="T2T",
                intent=deterministic_workflow_intent("T2T"),
            )
            with_ont = apply_asset_answer(
                draft,
                answer=f"ONT: {ont}",
                focused_role="ont_reads",
            ).draft
            no_hic = apply_asset_answer(
                with_ont,
                answer="没有 Hi-C",
                focused_role="hic_reads",
            ).draft

            clarification = next_intake_clarification(no_hic)
            self.assertEqual(clarification.kind, "degradation_confirmation")
            self.assertFalse(no_hic.degradation_decision)
            confirmed = confirm_no_hic_degradation(no_hic, confirmed=True)
            self.assertIsNone(next_intake_clarification(confirmed))
            manifest = materialize_manifest_draft(confirmed)
            self.assertEqual(manifest.user_metadata["hic_status"], "not_provided")
            self.assertTrue(manifest.user_metadata["hic_degradation_confirmed"])
            self.assertEqual(manifest.target_assembly_level, "contig")

    def test_rerun_child_revalidates_assets_and_resets_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ont = self._file(root, "ont.fastq")
            r1 = self._file(root, "r1.fastq")
            r2 = self._file(root, "r2.fastq")
            parent = create_tui_session_draft(
                session_id="parent",
                data_dir=str(root / "data"),
                request_text="T2T",
                intent=deterministic_workflow_intent("T2T"),
            )
            parent = apply_asset_answer(
                parent,
                answer=f"ONT: {ont}; Hi-C R1: {r1}; R2: {r2}",
                focused_role="ont_reads",
            ).draft
            parent = parent.model_copy(
                update={
                    "selected_stages": ["assembly", "hic_scaffolding"],
                    "resolved_execution_order": ["assembly", "hic_scaffolding"],
                    "selected_methods": {
                        "assembly": "assembly.candidate.flye",
                    },
                    "lifecycle": "terminal",
                }
            )
            parent_snapshot = parent.model_dump(mode="json")

            child = create_rerun_tui_session_draft(
                parent,
                session_id="child",
            )

            self.assertEqual(parent.model_dump(mode="json"), parent_snapshot)
            self.assertEqual(child.parent_session_id, "parent")
            self.assertEqual(child.lifecycle, "intake")
            self.assertIsNone(child.selected_stages)
            self.assertEqual(child.intake.ont_reads.paths, [str(ont.resolve())])
            self.assertEqual(child.intake.hic_reads.pairs[0].r1, str(r1.resolve()))

            r2.unlink()
            stale_child = create_rerun_tui_session_draft(
                parent,
                session_id="child-stale",
            )
            self.assertIsNone(stale_child.intake.hic_reads)
            self.assertTrue(stale_child.intake.unresolved_candidates)

    def test_rerun_hic_reuse_is_all_or_nothing_and_retains_every_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pair1 = (
                self._file(root, "pair1-r1.fastq"),
                self._file(root, "pair1-r2.fastq"),
            )
            pair2 = (
                self._file(root, "pair2-r1.fastq"),
                self._file(root, "pair2-r2.fastq"),
            )
            parent = create_tui_session_draft(
                session_id="parent",
                data_dir=str(root / "data"),
                request_text="T2T",
                intent=deterministic_workflow_intent("T2T"),
            )
            intake = parent.intake.model_copy(
                update={
                    "hic_reads": HiCPairedReadsFact(
                        pairs=[
                            HiCReadPair(r1=str(pair1[0]), r2=str(pair1[1])),
                            HiCReadPair(r1=str(pair2[0]), r2=str(pair2[1])),
                        ]
                    )
                }
            )
            parent = parent.model_copy(update={"intake": intake})
            pair2[1].unlink()

            child = create_rerun_tui_session_draft(parent, session_id="child")

            self.assertIsNone(child.intake.hic_reads)
            unresolved_paths = {
                path
                for item in child.intake.unresolved_candidates
                for path in item.candidate.paths
            }
            self.assertEqual(
                unresolved_paths,
                {str(path) for pair in (pair1, pair2) for path in pair},
            )

    def test_draft_persistence_round_trip_and_manifest_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reads = self._file(root, "reads.fastq")
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text="assembly",
                intent=deterministic_workflow_intent("assembly"),
            )
            draft = apply_asset_answer(
                draft,
                answer=f"ONT: {reads}",
                focused_role="ont_reads",
            ).draft
            state_path, manifest_path = save_tui_session_draft(draft)
            loaded = load_tui_session_draft(str(root / "data"), "s1")

            self.assertEqual(loaded, draft)
            self.assertTrue(state_path.exists())
            self.assertTrue(manifest_path.exists())
            self.assertIn(str(reads), manifest_path.read_text(encoding="utf-8"))

    def test_draft_persistence_canonicalizes_and_binds_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "nested" / ".." / "data"
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(data_dir),
                request_text="assembly",
            )

            save_tui_session_draft(draft)

            self.assertEqual(draft.data_dir, str((root / "data").resolve()))
            with self.assertRaisesRegex(ValueError, "data_dir"):
                load_tui_session_draft(root / "other-data", "s1")

    def test_stale_draft_save_is_rejected_by_revision_cas(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(data_dir),
                request_text="assembly",
            )
            save_tui_session_draft(draft)
            stale = load_tui_session_draft(data_dir, "s1")
            winner = load_tui_session_draft(data_dir, "s1").model_copy(
                update={"lifecycle": "planning"}
            )
            save_tui_session_draft(winner)

            with self.assertRaises(TuiDraftConflictError):
                save_tui_session_draft(stale)

            self.assertEqual(load_tui_session_draft(data_dir, "s1").lifecycle, "planning")
            self.assertEqual(winner.revision, 2)

    def test_manifest_pairs_are_explicit_and_paths_must_match(self):
        pair = PairedReadSpec(r1="/data/r1.fastq", r2="/data/r2.fastq")
        asset = InputAssetSpec(
            asset_type="hic_reads_registered",
            paths=[pair.r1, pair.r2],
            paired_reads=[pair],
            layout="paired",
            format="fastq",
        )
        self.assertEqual(asset.paths, ["/data/r1.fastq", "/data/r2.fastq"])
        with self.assertRaises(ValueError):
            InputAssetSpec(
                asset_type="hic_reads_registered",
                paths=["/data/other.fastq", "/data/r2.fastq"],
                paired_reads=[pair],
                layout="paired",
                format="fastq",
            )


if __name__ == "__main__":
    unittest.main()
