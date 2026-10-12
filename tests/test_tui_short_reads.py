from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.control.workflow_plan import workflow_steps_from_run_config
from app.entrypoints.ask_planner import (
    build_initial_plan_draft,
    build_run_config_payload,
)
from app.entrypoints.tui import _planner_state_from_tui_draft
from app.entrypoints.tui_intake import (
    apply_asset_answer,
    create_rerun_tui_session_draft,
    create_tui_session_draft,
    deterministic_workflow_intent,
    load_tui_session_draft,
    materialize_manifest_draft,
    parse_asset_answer_deterministically,
    save_tui_session_draft,
)
from app.schemas.ask_plan import PlanStageOccurrence


class TuiShortReadsTests(unittest.TestCase):
    def _read(self, root: Path, name: str) -> Path:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
        return path

    def test_pairing_requires_explicit_roles_and_not_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r1 = self._read(root, "sample_R1.fastq.gz")
            r2 = self._read(root, "sample_R2.fastq.gz")
            parsed = parse_asset_answer_deterministically(
                f"Illumina: {r1} {r2}", focused_role="short_reads"
            )
            self.assertEqual(parsed.candidates, [])
            self.assertEqual(len(parsed.unresolved_paths), 2)

            parsed = parse_asset_answer_deterministically(
                f"Illumina R1: {r1}; R2: {r2}", focused_role="short_reads"
            )
            self.assertEqual(
                [candidate.role for candidate in parsed.candidates],
                ["short_r1", "short_r2"],
            )

    def test_materialize_save_and_rerun_short_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            r1 = self._read(root, "reads/one.fastq.gz")
            r2 = self._read(root, "reads/two.fastq.gz")
            intent = deterministic_workflow_intent("Illumina short reads QC")
            draft = create_tui_session_draft(
                session_id="s1",
                data_dir=str(data_dir),
                request_text=intent.goal_text,
                intent=intent,
            )
            result = apply_asset_answer(
                draft,
                answer=f"Illumina R1: {r1}; R2: {r2}",
                focused_role="short_reads",
            )
            self.assertEqual(result.accepted_roles, ["short_reads"])
            asset = materialize_manifest_draft(result.draft).assets[0]
            self.assertEqual(asset.asset_type, "raw_short_dna_reads")
            self.assertEqual(asset.platform, "illumina")
            self.assertEqual(asset.layout, "paired")
            self.assertEqual(asset.metadata["intake_role"], "short_reads")
            self.assertEqual(asset.paired_reads[0].r1, str(r1.resolve()))
            save_tui_session_draft(result.draft)
            loaded = load_tui_session_draft(data_dir, "s1")
            self.assertEqual(loaded.intake.short_reads, result.draft.intake.short_reads)

            child = create_rerun_tui_session_draft(
                loaded, session_id="s2", intent=intent, request_text=intent.goal_text
            )
            self.assertEqual(child.intake.short_reads, loaded.intake.short_reads)

    def test_illumina_qc_and_survey_do_not_require_ont(self):
        qc = deterministic_workflow_intent("only Illumina reads for QC")
        survey = deterministic_workflow_intent("Illumina QC and genome survey")
        self.assertEqual(qc.requested_stages, ["read_qc"])
        self.assertEqual(survey.requested_stages, ["read_qc", "genome_survey"])
        self.assertEqual(
            qc
            and create_tui_session_draft(
                session_id="s", data_dir="data", request_text=qc.goal_text, intent=qc
            ).intake.requirements,
            ["short_reads"],
        )

    def test_hifi_and_illumina_are_collected_without_short_read_assembly(self):
        intent = deterministic_workflow_intent("HiFi plus Illumina reads assembly")
        self.assertEqual(intent.requested_stages[0], "read_qc")
        draft = create_tui_session_draft(
            session_id="s",
            data_dir="data",
            request_text=intent.goal_text,
            intent=intent,
        )
        self.assertEqual(draft.intake.requirements, ["hifi_reads", "short_reads"])
        self.assertNotEqual(intent.requested_stages, ["read_qc"])
        plan = _planner_state_from_tui_draft(draft)
        self.assertIsNone(plan.entry_request["platform"])
        self.assertNotIn("short_reads", plan.entry_request.get("hifiasm_inputs", {}))

    def test_short_reads_flow_through_ask_plan_run_config_and_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r1 = self._read(root, "reads/r1.fastq.gz")
            r2 = self._read(root, "reads/r2.fastq.gz")
            intent = deterministic_workflow_intent("Illumina only QC")
            draft = create_tui_session_draft(
                session_id="s",
                data_dir=str(root / "data"),
                request_text=intent.goal_text,
                intent=intent,
            )
            draft = apply_asset_answer(
                draft,
                answer=f"Illumina R1: {r1}; R2: {r2}",
                focused_role="short_reads",
            ).draft
            save_tui_session_draft(draft)

            plan = build_initial_plan_draft(_planner_state_from_tui_draft(draft))
            self.assertIsNotNone(plan)
            assert plan is not None
            payload = build_run_config_payload(plan)
            self.assertIn("short_reads", payload["read_qc"])
            self.assertNotIn("hic_reads", payload["read_qc"])

            from app.schemas.run_config import RunConfig

            run_config = RunConfig.model_validate(payload)
            read_qc_step = workflow_steps_from_run_config(run_config)[1]
            self.assertEqual(read_qc_step["stage"], "read_qc")
            self.assertIn(
                "read_qc.short_reads.fastp",
                read_qc_step["selected_method_names"],
            )

    def test_trio_parent_short_assets_do_not_enable_child_fastp(self):
        from app.schemas.tui_intake import (
            TrioMaternalReadsFact,
            TrioPaternalReadsFact,
        )

        intent = deterministic_workflow_intent("Trio hifiasm assembly")
        draft = create_tui_session_draft(
            session_id="s",
            data_dir="data",
            request_text=intent.goal_text,
            intent=intent,
        )
        intake = draft.intake.model_copy(
            update={
                "trio_maternal_reads": TrioMaternalReadsFact(paths=["/m.fastq"]),
                "trio_paternal_reads": TrioPaternalReadsFact(paths=["/p.fastq"]),
            }
        )
        plan = _planner_state_from_tui_draft(
            draft.model_copy(update={"intake": intake})
        )
        plan = plan.model_copy(
            update={
                "selected_stage_occurrences": [
                    PlanStageOccurrence(stage="read_qc", allow_local_execution=True)
                ]
            }
        )
        from app.entrypoints.ask_planner import build_run_config_payload

        payload = build_run_config_payload(plan)
        self.assertNotIn("short_reads", payload["read_qc"])

    def test_confirmed_hic_is_compiled_to_hic_fastp_roster(self):
        from app.schemas.ask_plan import PlanStageOccurrence
        from app.schemas.tui_intake import HiCPairedReadsFact, HiCReadPair

        intent = deterministic_workflow_intent("HiFi assembly with Hi-C phasing")
        draft = create_tui_session_draft(
            session_id="s",
            data_dir="data",
            request_text=intent.goal_text,
            intent=intent,
        )
        intake = draft.intake.model_copy(
            update={
                "hic_reads": HiCPairedReadsFact(
                    pairs=[HiCReadPair(r1="/hic/r1.fastq", r2="/hic/r2.fastq")]
                )
            }
        )
        plan = _planner_state_from_tui_draft(
            draft.model_copy(update={"intake": intake})
        )
        plan = plan.model_copy(
            update={
                "selected_stage_occurrences": [
                    PlanStageOccurrence(stage="read_qc", allow_local_execution=True)
                ]
            }
        )
        payload = build_run_config_payload(plan)
        self.assertIn("hic_reads", payload["read_qc"])
        from app.schemas.run_config import RunConfig

        roster = workflow_steps_from_run_config(RunConfig.model_validate(payload))[1][
            "selected_method_names"
        ]
        self.assertIn("read_qc.hic_reads.fastp", roster)

    def test_ultralong_ont_companion_hint_requests_ont_without_hifiasm_name(self):
        intent = deterministic_workflow_intent(
            "assembly using HiFi with ultra-long ONT companion reads"
        )
        draft = create_tui_session_draft(
            session_id="s",
            data_dir="data",
            request_text=intent.goal_text,
            intent=intent,
        )
        self.assertEqual(draft.intake.requirements, ["hifi_reads", "ont_reads"])


if __name__ == "__main__":
    unittest.main()
