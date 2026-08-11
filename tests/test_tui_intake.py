"""Focused tests for the TUI's pre-freeze sample intake core."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.entrypoints.ask_planner import _build_assembly_execution_config
from app.entrypoints.tui import (
    _apply_llm_plan_draft,
    _planner_state_from_tui_draft,
)
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
from app.schemas.ask_plan import PlanStageOccurrence
from app.schemas.manifest import InputAssetSpec, PairedReadSpec
from app.schemas.run_config import RunConfig
from app.schemas.tui_intake import HiCPairedReadsFact, HiCReadPair, HifiReadsFact
from tests.busco_fixture import assembly_review_config


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

    def test_t2t_goal_recommends_distinct_post_t2t_assembly_review(self):
        intent = deterministic_workflow_intent("为该样本做 T2T 组装")

        self.assertEqual(
            intent.requested_stages,
            [
                "read_qc",
                "genome_survey",
                "assembly",
                "assembly_dedup",
                "assembly_review",
                "hic_scaffolding",
                "t2t",
                "assembly_review",
            ],
        )

    def test_assembly_defaults_close_the_mandatory_review_boundary(self):
        for request_text, target_level in (
            ("assemble a genome", "contig"),
            ("scaffold this genome", "scaffold"),
        ):
            with self.subTest(request_text=request_text):
                intent = deterministic_workflow_intent(request_text)

                self.assertEqual(intent.target_assembly_level, target_level)
                self.assertEqual(
                    intent.requested_stages,
                    [
                        "read_qc",
                        "genome_survey",
                        "assembly",
                        "assembly_dedup",
                        "assembly_review",
                    ],
                )
                RunConfig.model_validate(
                    {
                        "requested_stages": [
                            {"stage": stage, "allow_local_execution": False}
                            for stage in intent.requested_stages
                        ],
                        "assembly_review": assembly_review_config(
                            "/data/busco/embryophyta_odb10"
                        ),
                    }
                )

    def test_hifiasm_goal_collects_primary_and_enabled_companion_roles(self):
        intent = deterministic_workflow_intent(
            "用 Hifiasm 做 assembly，HiFi 作为主输入，"
            "通过 --ul 加入 ultra-long ONT，并用 Hi-C phasing"
        )
        draft = create_tui_session_draft(
            session_id="s1",
            data_dir="data",
            request_text=intent.goal_text,
            intent=intent,
        )

        self.assertEqual(intent.requested_assembly_method, "hifiasm")
        self.assertTrue(intent.use_ultralong_ont)
        self.assertTrue(intent.use_hic_phasing)
        self.assertEqual(
            draft.intake.requirements,
            ["hifi_reads", "ont_reads", "hic_reads"],
        )
        self.assertEqual(next_intake_clarification(draft).role, "hifi_reads")

    def test_hifiasm_ont_goal_is_a_distinct_ont_primary_method(self):
        intent = deterministic_workflow_intent("用 hifiasm-ont --ont 做 assembly")

        self.assertEqual(intent.requested_assembly_method, "hifiasm_ont")
        self.assertFalse(intent.use_ultralong_ont)
        self.assertFalse(intent.use_hic_phasing)
        self.assertEqual(
            create_tui_session_draft(
                session_id="s1",
                data_dir="data",
                request_text=intent.goal_text,
                intent=intent,
            ).intake.requirements,
            ["ont_reads"],
        )

    def test_hifiasm_intent_projects_into_existing_planner_and_run_config(self):
        intent = deterministic_workflow_intent(
            "Hifiasm assembly with --ul ONT and Hi-C"
        )
        draft = create_tui_session_draft(
            session_id="s1",
            data_dir="data",
            request_text=intent.goal_text,
            intent=intent,
        )
        intake = draft.intake.model_copy(
            update={"hifi_reads": HifiReadsFact(paths=["/reads/hifi.fastq.gz"])}
        )
        draft = draft.model_copy(update={"intake": intake})

        plan = _planner_state_from_tui_draft(draft)
        assembly = _build_assembly_execution_config(plan, ["assembly"])

        self.assertEqual(
            plan.selected_methods,
            {"assembly": "assembly.candidate.hifiasm"},
        )
        self.assertEqual(
            plan.method_parameters["assembly.candidate.hifiasm"],
            {
                "use_ultralong_ont": True,
                "use_hic_phasing": True,
            },
        )
        self.assertEqual(
            assembly,
            {
                "assembly_method": "hifiasm",
                "assembly_route": "hifi",
                "hifiasm_parameters": {
                    "use_ultralong_ont": True,
                    "use_hic_phasing": True,
                },
            },
        )

    def test_hifiasm_intake_materializes_separate_hifi_ont_and_multilane_hic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hifi = [
                self._file(root, "hifi/a.fastq.gz"),
                self._file(root, "hifi/b.fastq.gz"),
            ]
            ont = [
                self._file(root, "ont/a.fastq.gz"),
                self._file(root, "ont/b.fastq.gz"),
            ]
            r1 = [
                self._file(root, "hic/L1-R1.fastq.gz"),
                self._file(root, "hic/L2-R1.fastq.gz"),
            ]
            r2 = [
                self._file(root, "hic/L1-R2.fastq.gz"),
                self._file(root, "hic/L2-R2.fastq.gz"),
            ]
            intent = deterministic_workflow_intent(
                "Hifiasm assembly with --ul ONT and Hi-C"
            )
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(root / "data"),
                request_text=intent.goal_text,
                intent=intent,
            )
            answer = (
                f"HiFi: {hifi[0]} {hifi[1]}; "
                f"ONT: {ont[0]} {ont[1]}; "
                f"Hi-C R1: {r1[0]} {r1[1]}; "
                f"Hi-C R2: {r2[0]} {r2[1]}"
            )

            result = apply_asset_answer(
                draft,
                answer=answer,
                focused_role="hifi_reads",
            )
            manifest = materialize_manifest_draft(result.draft)

            self.assertEqual(
                result.accepted_roles,
                ["hifi_reads", "ont_reads", "hic_reads"],
            )
            long_reads = [
                asset
                for asset in manifest.assets
                if asset.asset_type == "raw_long_reads"
            ]
            self.assertEqual(
                [asset.platform for asset in long_reads],
                ["pacbio_hifi", "ont"],
            )
            self.assertEqual([len(asset.paths) for asset in long_reads], [2, 2])
            hic = next(
                asset
                for asset in manifest.assets
                if asset.asset_type == "hic_reads_registered"
            )
            self.assertEqual(len(hic.paired_reads), 2)
            self.assertEqual(
                [(pair.r1, pair.r2) for pair in hic.paired_reads],
                [
                    (str(r1[0].resolve()), str(r2[0].resolve())),
                    (str(r1[1].resolve()), str(r2[1].resolve())),
                ],
            )

    def test_llm_cannot_narrow_t2t_intent_to_unreviewed_assembly(self):
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
        self.assertEqual(
            intent.requested_stages,
            deterministic_workflow_intent(request).requested_stages,
        )

    def test_llm_cannot_drop_mandatory_review_from_assembly_intent(self):
        intent = parse_workflow_intent(
            "assembly",
            llm_parser=lambda _request: {
                "goal_text": "ignored",
                "requested_stages": ["assembly"],
            },
        )

        self.assertEqual(
            intent.requested_stages,
            deterministic_workflow_intent("assembly").requested_stages,
        )

    def test_llm_intent_cannot_expand_explicit_stage_scope(self):
        intent = parse_workflow_intent(
            "assembly",
            llm_parser=lambda _request: {
                "goal_text": "ignored",
                "requested_stages": ["assembly", "t2t"],
            },
        )

        self.assertEqual(
            intent.requested_stages,
            deterministic_workflow_intent("assembly").requested_stages,
        )

    def test_llm_preserves_custom_order_and_repeated_stages_without_scope(self):
        custom_sequence = [
            "t2t",
            "assembly_review",
            "assembly",
            "assembly_review",
            "t2t",
            "assembly_review",
        ]
        intent = parse_workflow_intent(
            "custom workflow",
            llm_parser=lambda _request: {
                "goal_text": "ignored",
                "requested_stages": custom_sequence,
            },
        )

        self.assertEqual(intent.requested_stages, custom_sequence)

    def test_llm_invalid_custom_sequence_falls_back_to_inventory_only(self):
        intent = parse_workflow_intent(
            "custom workflow",
            llm_parser=lambda _request: {
                "goal_text": "ignored",
                "requested_stages": ["t2t"],
            },
        )

        self.assertEqual(intent.requested_stages, [])

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
            self.assertEqual(confirmed.intent.target_assembly_level, "contig")
            self.assertFalse(confirmed.intent.use_hic_phasing)
            self.assertNotIn("hic_reads", confirmed.intake.requirements)
            self.assertEqual(
                confirmed.intent.requested_stages,
                [
                    "read_qc",
                    "genome_survey",
                    "assembly",
                    "assembly_dedup",
                    "assembly_review",
                ],
            )
            planner_state = _planner_state_from_tui_draft(confirmed)
            requested_scope = planner_state.entry_request["requested_stages"]
            self.assertEqual(requested_scope, confirmed.intent.requested_stages)
            planned = _apply_llm_plan_draft(
                planner_state,
                {
                    "selected_stage_occurrences": [
                        {"stage": stage, "allow_local_execution": False}
                        for stage in requested_scope
                    ],
                    "selected_methods": {},
                },
            )
            self.assertEqual(planned.selected_stage_names, requested_scope)
            manifest = materialize_manifest_draft(confirmed)
            self.assertEqual(manifest.user_metadata["hic_status"], "not_provided")
            self.assertTrue(manifest.user_metadata["hic_degradation_confirmed"])
            self.assertNotIn(
                "target_assembly_level", manifest.model_dump(mode="json")
            )

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
                    "selected_stage_occurrences": [
                        PlanStageOccurrence(
                            stage="assembly", allow_local_execution=True
                        ),
                        PlanStageOccurrence(
                            stage="hic_scaffolding", allow_local_execution=True
                        ),
                    ],
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
            self.assertIsNone(child.selected_stage_names)
            self.assertEqual(child.intake.ont_reads.paths, [str(ont.resolve())])
            self.assertEqual(child.intake.hic_reads.pairs[0].r1, str(r1.resolve()))

            r2.unlink()
            stale_child = create_rerun_tui_session_draft(
                parent,
                session_id="child-stale",
            )
            self.assertIsNone(stale_child.intake.hic_reads)
            self.assertTrue(stale_child.intake.unresolved_candidates)

    def test_planner_state_keeps_occurrence_local_execution_consent(self):
        draft = create_tui_session_draft(
            session_id="s1",
            data_dir="data",
            request_text="custom workflow",
        ).model_copy(
            update={
                "selected_stage_occurrences": [
                    PlanStageOccurrence(stage="t2t", allow_local_execution=False),
                    PlanStageOccurrence(
                        stage="assembly_review", allow_local_execution=True
                    ),
                    PlanStageOccurrence(stage="t2t", allow_local_execution=True),
                ]
            }
        )

        plan = _planner_state_from_tui_draft(draft)

        self.assertEqual(
            [
                occurrence.model_dump(mode="json")
                for occurrence in plan.selected_stage_occurrences
            ],
            [
                {"stage": "t2t", "allow_local_execution": False},
                {"stage": "assembly_review", "allow_local_execution": True},
                {"stage": "t2t", "allow_local_execution": True},
            ],
        )

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

            self.assertEqual(
                load_tui_session_draft(data_dir, "s1").lifecycle,
                "planning",
            )
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
