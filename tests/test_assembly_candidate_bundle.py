"""Status-contract tests for Assembly candidate product manifests."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.schemas.artifacts import (
    ArtifactRef,
    AssemblyCandidateBundle,
    AssemblyNativeOutput,
)


def _ref(name: str, artifact_type: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"artifact-{name}",
        path_ref=f"artifacts/assembly/{name}",
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


if __name__ == "__main__":
    unittest.main()
