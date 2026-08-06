"""Status-contract tests for Assembly candidate product manifests."""

from __future__ import annotations

import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from app.schemas.artifacts import (
    ASSEMBLY_DRAFT_ASSEMBLY,
    HIC_SCAFFOLDED_ASSEMBLY,
    T2T_PATCHED_ASSEMBLY,
    ArtifactMeta,
    ArtifactRef,
    AssemblyCandidateBundle,
    AssemblyNativeOutput,
)
from app.storage.artifacts import publish_artifact_record
from app.storage.assembly_candidates import (
    publish_derived_assembly_candidate,
    publish_registered_assembly_candidate,
)
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


def _ref(name: str, artifact_type: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"artifact-{name}",
        path_ref=f"artifacts/steps/step-001-assembly/assembly/{name}",
        sha256="a" * 64,
        size_bytes=1,
        artifact_type=artifact_type,
    )


class AssemblyCandidateBundleTests(unittest.TestCase):
    def _planned(self) -> AssemblyCandidateBundle:
        return AssemblyCandidateBundle(
            session_id="s1",
            candidate_id="s1:assembly.candidate.hifiasm:parameter_attempt_0",
            method_name="assembly.candidate.hifiasm",
            status="planned",
            input_type="clean_long_reads",
            input_member_ids={
                "primary": ["hifi-a", "hifi-b"],
                "ultralong": ["ont-a"],
                "hic_r1": ["hic-r1"],
                "hic_r2": ["hic-r2"],
            },
            input_member_platforms={
                "primary": ["pacbio_hifi", "pacbio_hifi"],
                "ultralong": ["ont"],
                "hic_r1": ["illumina"],
                "hic_r2": ["illumina"],
            },
            parameter_attempt_id=0,
            expected_outputs=["primary_assembly"],
        )

    def test_planned_bundle_rejects_materialized_outputs(self):
        payload = self._planned().model_dump(mode="json")
        payload["primary_output"] = _ref(
            "draft.fasta",
            "assembly.draft_assembly",
        ).model_dump(mode="json")

        with self.assertRaisesRegex(
            ValidationError,
            "planned candidate bundle cannot contain materialized output refs",
        ):
            AssemblyCandidateBundle.model_validate(payload)

    def test_executed_bundle_requires_plan_and_primary_refs(self):
        payload = self._planned().model_dump(mode="json")
        payload["status"] = "executed"

        with self.assertRaisesRegex(
            ValidationError,
            "requires plan_ref and primary_output",
        ):
            AssemblyCandidateBundle.model_validate(payload)

    def test_executed_bundle_accepts_generic_native_outputs(self):
        payload = self._planned().model_dump(mode="json")
        payload.update(
            {
                "status": "executed",
                "plan_ref": _ref(
                    "candidate.planned.json",
                    "draft_assembly.candidate",
                ).model_dump(mode="json"),
                "primary_output": _ref(
                    "draft.fasta",
                    "assembly.draft_assembly",
                ).model_dump(mode="json"),
                "native_outputs": [
                    AssemblyNativeOutput(
                        artifact_class="assembly_graph",
                        native_role="another_assembler.primary_graph",
                        media_type="application/gfa",
                        artifact_ref=_ref(
                            "native.gfa",
                            "assembly.another_assembler_gfa",
                        ),
                    ).model_dump(mode="json")
                ],
            }
        )

        executed = AssemblyCandidateBundle.model_validate(payload)

        self.assertEqual(
            executed.native_outputs[0].native_role,
            "another_assembler.primary_graph",
        )

    def test_bundle_rejects_duplicate_native_roles(self):
        payload = self._planned().model_dump(mode="json")
        native = AssemblyNativeOutput(
            artifact_class="assembly_graph",
            native_role="another_assembler.primary_graph",
            media_type="application/gfa",
            artifact_ref=_ref(
                "native.gfa",
                "assembly.another_assembler_gfa",
            ),
        ).model_dump(mode="json")
        payload.update(
            {
                "status": "executed",
                "plan_ref": _ref(
                    "candidate.planned.json",
                    "draft_assembly.candidate",
                ).model_dump(mode="json"),
                "primary_output": _ref(
                    "draft.fasta",
                    "assembly.draft_assembly",
                ).model_dump(mode="json"),
                "native_outputs": [native, native],
            }
        )

        with self.assertRaisesRegex(
            ValidationError,
            "duplicate native output role",
        ):
            AssemblyCandidateBundle.model_validate(payload)

    def test_bundle_rejects_unimplemented_failed_status(self):
        payload = self._planned().model_dump(mode="json")
        payload["status"] = "failed"

        with self.assertRaises(ValidationError):
            AssemblyCandidateBundle.model_validate(payload)

    def test_derived_candidates_accept_each_canonical_primary_type(self):
        for artifact_type in (
            ASSEMBLY_DRAFT_ASSEMBLY,
            HIC_SCAFFOLDED_ASSEMBLY,
            T2T_PATCHED_ASSEMBLY,
        ):
            with (
                self.subTest(artifact_type=artifact_type),
                TemporaryDirectory() as tmpdir,
            ):
                data_dir = v2_run_dir(tmpdir, "s1")
                source = Path(tmpdir) / "source.fasta"
                source.write_text(">source\nACGT\n", encoding="utf-8")
                paths = SessionPaths.from_run_dir(data_dir)
                registry_source = paths.workflow_step_work_path(
                    "step-000-inventory", "inventory", "registry.json"
                )
                registry_source.parent.mkdir(parents=True, exist_ok=True)
                registry_source.write_text("{}\n", encoding="utf-8")
                registry_ref = publish_artifact_record(
                    data_dir,
                    "s1",
                    ArtifactMeta.from_file(
                        registry_source,
                        stage="inventory",
                        method_name="test.registry",
                        artifact_type="asset_registry",
                    ),
                ).ref
                source_candidate = publish_registered_assembly_candidate(
                    data_dir,
                    "s1",
                    workflow_step_id="step-000-inventory",
                    asset_id="source",
                    source_path=source,
                    expected_sha256=sha256(source.read_bytes()).hexdigest(),
                    expected_size_bytes=source.stat().st_size,
                    asset_registry_ref=registry_ref,
                )
                primary_source = paths.workflow_step_work_path(
                    "step-001-derived", "t2t", "primary.fasta"
                )
                primary_source.parent.mkdir(parents=True, exist_ok=True)
                primary_source.write_text(">derived\nACGTA\n", encoding="utf-8")
                primary_ref = publish_artifact_record(
                    data_dir,
                    "s1",
                    ArtifactMeta.from_file(
                        primary_source,
                        stage="t2t",
                        method_name="test.derived",
                        artifact_type=artifact_type,
                    ),
                ).ref

                derived = publish_derived_assembly_candidate(
                    data_dir,
                    "s1",
                    workflow_step_id="step-001-derived",
                    source_candidate=source_candidate,
                    primary_output_ref=primary_ref,
                    stage="t2t",
                    method_name="test.derived",
                    action_identity="decision-artifact-id",
                )

                self.assertEqual(derived.primary_output_ref, primary_ref)

    def test_derived_candidate_replay_is_stable_and_conflicts_fail_closed(self):
        with TemporaryDirectory() as tmpdir:
            data_dir = v2_run_dir(tmpdir, "s1")
            source = Path(tmpdir) / "source.fasta"
            source.write_text(">source\nACGT\n", encoding="utf-8")
            paths = SessionPaths.from_run_dir(data_dir)
            registry_source = paths.workflow_step_work_path(
                "step-000-inventory", "inventory", "registry.json"
            )
            registry_source.parent.mkdir(parents=True, exist_ok=True)
            registry_source.write_text("{}\n", encoding="utf-8")
            registry_ref = publish_artifact_record(
                data_dir,
                "s1",
                ArtifactMeta.from_file(
                    registry_source,
                    stage="inventory",
                    method_name="test.registry",
                    artifact_type="asset_registry",
                ),
            ).ref
            source_candidate = publish_registered_assembly_candidate(
                data_dir,
                "s1",
                workflow_step_id="step-000-inventory",
                asset_id="source",
                source_path=source,
                expected_sha256=sha256(source.read_bytes()).hexdigest(),
                expected_size_bytes=source.stat().st_size,
                asset_registry_ref=registry_ref,
            )

            def primary_ref(name: str, contents: str) -> ArtifactRef:
                primary_source = paths.workflow_step_work_path(
                    "step-001-derived", "t2t", name
                )
                primary_source.parent.mkdir(parents=True, exist_ok=True)
                primary_source.write_text(contents, encoding="utf-8")
                return publish_artifact_record(
                    data_dir,
                    "s1",
                    ArtifactMeta.from_file(
                        primary_source,
                        stage="t2t",
                        method_name="test.derived",
                        artifact_type=T2T_PATCHED_ASSEMBLY,
                    ),
                ).ref

            first_primary = primary_ref("first.fasta", ">one\nACGTA\n")
            first = publish_derived_assembly_candidate(
                data_dir,
                "s1",
                workflow_step_id="step-001-derived",
                source_candidate=source_candidate,
                primary_output_ref=first_primary,
                stage="t2t",
                method_name="test.derived",
                action_identity="persisted-decision",
            )
            replay = publish_derived_assembly_candidate(
                data_dir,
                "s1",
                workflow_step_id="step-001-derived",
                source_candidate=source_candidate,
                primary_output_ref=first_primary,
                stage="t2t",
                method_name="test.derived",
                action_identity="persisted-decision",
            )
            self.assertEqual(replay.ref, first.ref)

            conflicting_primary = primary_ref("conflict.fasta", ">two\nACGTAA\n")
            with self.assertRaises(OSError):
                publish_derived_assembly_candidate(
                    data_dir,
                    "s1",
                    workflow_step_id="step-001-derived",
                    source_candidate=source_candidate,
                    primary_output_ref=conflicting_primary,
                    stage="t2t",
                    method_name="test.derived",
                    action_identity="persisted-decision",
                )


if __name__ == "__main__":
    unittest.main()
