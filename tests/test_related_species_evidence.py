"""Tests for local NCBI related-species reference evidence."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.reference.ncbi import build_related_species_evidence_pack
from app.review.persistence import load_review_request
from app.schemas.state import initial_state
from app.stages.assembly_review.nodes import (
    build_related_species_evidence_pack as build_review_evidence_pack,
)
from app.stages.assembly_review.nodes import human_review_gate
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir

_HEADERS = [
    "Assembly Accession",
    "Current Accession",
    "Assembly Name",
    "Organism Name",
    "Organism Taxonomic ID",
    "Source Database",
    "Assembly Assembly Method",
    "Assembly Stats Total Number of Chromosomes",
    "Assembly Stats Total Sequence Length",
    "Assembly Stats Number of Contigs",
    "Assembly Stats Number of Scaffolds",
    "Assembly Stats Contig N50",
    "Assembly Stats Scaffold N50",
    "Assembly Stats GC Percent",
    "Annotation BUSCO Lineage",
    "Assembly Type",
]

_ASSEMBLY_REVIEW_WORKFLOW_STEPS = [
    {
        "step_id": "step-000-inventory",
        "stage": "inventory",
        "allow_local_execution": False,
        "selected_method_names": [],
    },
    {
        "step_id": "step-001-assembly",
        "stage": "assembly",
        "allow_local_execution": True,
    },
    {
        "step_id": "step-002-assembly_review",
        "stage": "assembly_review",
        "allow_local_execution": True,
        "selected_method_names": [
            "assembly_review.eval.quast",
            "assembly_review.eval.busco",
        ],
        "review_target": {
            "resource_type": "assembly",
            "producer_step_id": "step-001-assembly",
        },
    },
    {
        "step_id": "step-003-hic_scaffolding",
        "stage": "hic_scaffolding",
        "allow_local_execution": True,
        "selected_method_names": ["hic_scaffolding.candidate.autohic"],
    },
]


def _candidate(candidate_id: str, method_name: str = "flye") -> dict:
    return {
        "candidate_id": candidate_id,
        "method_name": method_name,
        "source": "test_fixture",
        "status": "executed",
        "bundle_ref": {
            "schema_version": 2,
            "artifact_id": f"bundle:{candidate_id}",
            "path_ref": (
                "artifacts/steps/step-001-assembly/assembly/"
                f"candidates/{candidate_id}/candidate.json"
            ),
            "sha256": "0" * 64,
            "size_bytes": 0,
            "artifact_type": "draft_assembly.candidate",
        },
        "primary_output_ref": {
            "schema_version": 2,
            "artifact_id": f"primary:{candidate_id}",
            "path_ref": (
                "artifacts/steps/step-001-assembly/assembly/"
                f"candidates/{candidate_id}/draft.fa"
            ),
            "sha256": "1" * 64,
            "size_bytes": 0,
            "artifact_type": "assembly.draft_assembly",
        },
    }


class RelatedSpeciesEvidenceTests(unittest.TestCase):
    def _state(
        self, tmpdir: str, manifest_text: str, *, session_id: str = "s1"
    ) -> dict:
        run_dir = v2_run_dir(tmpdir, session_id)
        (run_dir / "control" / "manifest.yaml").write_text(
            manifest_text,
            encoding="utf-8",
        )
        state = initial_state(
            session_id=session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
            workflow_steps=_ASSEMBLY_REVIEW_WORKFLOW_STEPS,
            stage_plan_ref="control/run_config.json",
            stage_seeds={"assembly_review": {"status": "pending"}},
        )
        state.update(
            {
                "workflow_step_index": 2,
                "entered_workflow_step_index": 2,
                "current_stage": "assembly_review",
            }
        )
        return state

    def _reference_dir(self, tmpdir: str) -> Path:
        root = Path(tmpdir) / "ncbi_metazoa"
        root.mkdir()
        with (root / "species_alias.tsv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "chinese_name",
                    "canonical_scientific_name",
                    "aliases",
                    "genus",
                    "family",
                    "order",
                    "notes",
                ],
                delimiter="\t",
            )
            writer.writeheader()
            writer.writerow(
                {
                    "chinese_name": "目标鱼",
                    "canonical_scientific_name": "Targetus absentus",
                    "aliases": "target fish",
                    "genus": "Targetus",
                    "family": "Targetidae",
                    "order": "Targetiformes",
                }
            )
        return root

    def _review_reference_dir(self, tmpdir: str) -> Path:
        """Create a complete local snapshot for the isolated node seams."""
        root = self._reference_dir(tmpdir)
        with (root / "species_alias.tsv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "chinese_name",
                    "canonical_scientific_name",
                    "aliases",
                    "genus",
                    "family",
                    "order",
                    "notes",
                ],
                delimiter="\t",
            )
            writer.writeheader()
            writer.writerow(
                {
                    "chinese_name": "卵鳎",
                    "canonical_scientific_name": "Solea ovata",
                    "aliases": "tongue sole",
                    "genus": "Solea",
                    "family": "Soleidae",
                    "order": "Pleuronectiformes",
                }
            )
        self._write_records(
            root,
            [
                self._row("GCF_000000101.1", "Solea senegalensis", "101"),
                self._row("GCF_000000102.1", "Solea solea", "102"),
                self._row("GCF_000000103.1", "Solea vulgaris", "103"),
            ],
            {
                "101": "Metazoa;Pleuronectiformes;Soleidae;Solea;Solea senegalensis",
                "102": "Metazoa;Pleuronectiformes;Soleidae;Solea;Solea solea",
                "103": "Metazoa;Pleuronectiformes;Soleidae;Solea;Solea vulgaris",
            },
        )
        return root

    def _write_records(
        self,
        root: Path,
        rows: list[dict[str, str]],
        lineage: dict[str, str],
        *,
        lineage_header: str = "Lineage",
    ) -> None:
        with (root / "Metazoa.ncbi.tsv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=_HEADERS, delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)
        with (root / "Metazoa.ncbi.lineage").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["Organism Taxonomic ID", lineage_header],
                delimiter="\t",
            )
            writer.writeheader()
            for taxid, value in lineage.items():
                writer.writerow({"Organism Taxonomic ID": taxid, lineage_header: value})
        self._write_manifest(root)

    def _write_manifest(self, root: Path) -> None:
        files = {
            name: {
                "sha256": self._sha256(root / name),
                "size_bytes": (root / name).stat().st_size,
            }
            for name in (
                "Metazoa.ncbi.tsv",
                "Metazoa.ncbi.lineage",
                "species_alias.tsv",
            )
        }
        (root / "reference_manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "dataset_id": "test_ncbi_metazoa",
                    "source_provider": "test",
                    "source_version": "test-v1",
                    "exported_at": "2026-06-23T00:00:00Z",
                    "files": files,
                },
            ),
            encoding="utf-8",
        )

    def _sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _row(
        self,
        accession: str,
        organism: str,
        taxid: str,
        *,
        assembly: str = "primary",
        scaffold_n50: str = "100",
        contig_n50: str = "10",
    ) -> dict[str, str]:
        return {
            "Assembly Accession": accession,
            "Current Accession": accession,
            "Assembly Name": assembly,
            "Organism Name": organism,
            "Organism Taxonomic ID": taxid,
            "Source Database": "SOURCE_DATABASE_GENBANK",
            "Assembly Assembly Method": "assembler",
            "Assembly Stats Total Number of Chromosomes": "21",
            "Assembly Stats Total Sequence Length": "600",
            "Assembly Stats Number of Contigs": "20",
            "Assembly Stats Number of Scaffolds": "10",
            "Assembly Stats Contig N50": contig_n50,
            "Assembly Stats Scaffold N50": scaffold_n50,
            "Assembly Stats GC Percent": "41",
            "Annotation BUSCO Lineage": "actinopterygii_odb10",
        }

    def test_same_genus_deduplicates_gcf_gca_and_marks_alternates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [
                    self._row(
                        "GCA_000000001.1", "Targetus relative", "1", scaffold_n50="200"
                    ),
                    self._row(
                        "GCF_000000001.1", "Targetus relative", "1", scaffold_n50="100"
                    ),
                    self._row(
                        "GCA_000000002.1",
                        "Targetus relative",
                        "1",
                        assembly="sample alternate haplotype",
                        scaffold_n50="999",
                    ),
                ],
                {"1": "Metazoa;Targetiformes;Targetidae;Targetus;Targetus relative"},
            )

            pack = build_related_species_evidence_pack("目标鱼", reference_dir=root)

            self.assertEqual(pack["matching_mode"], "same_genus")
            self.assertEqual(
                [record["accession"] for record in pack["selected_records"]],
                ["GCF_000000001.1"],
            )
            self.assertEqual(
                pack["excluded_alternate_haplotype_records"][0]["accession"],
                "GCA_000000002.1",
            )
            self.assertEqual(pack["computed_summary"]["selected_record_count"], 1)
            self.assertEqual(
                pack["selected_records"][0]["busco"]["lineage"], "actinopterygii_odb10"
            )

    def test_hap1_hap2_and_parental_haplotypes_never_enter_primary_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [
                    self._row(
                        "GCA_000000011.1",
                        "Abia aenea",
                        "11",
                        assembly="Abia_aenea.primary",
                    ),
                    self._row(
                        "GCA_000000012.1",
                        "Abia aenea",
                        "11",
                        assembly="Abia_aenea.hap1",
                    ),
                    self._row(
                        "GCA_000000013.1",
                        "Abia aenea",
                        "11",
                        assembly="Abia_aenea_hap2",
                    ),
                    self._row(
                        "GCA_000000014.1",
                        "Abia aenea",
                        "11",
                        assembly="Abia aenea maternal haplotype",
                    ),
                    self._row(
                        "GCA_000000015.1",
                        "Abia aenea",
                        "11",
                        assembly="Abia aenea paternal haplotype",
                    ),
                    {
                        **self._row(
                            "GCA_000000016.1",
                            "Abia aenea",
                            "11",
                            assembly="Abia_aenea.clean_name",
                        ),
                        "Assembly Type": "alternate-pseudohaplotype",
                    },
                ],
                {"11": "Metazoa;Hymenoptera;Tenthredinidae;Abia;Abia aenea"},
            )

            pack = build_related_species_evidence_pack("Abia aenea", reference_dir=root)

            self.assertEqual(
                [record["accession"] for record in pack["selected_records"]],
                ["GCA_000000011.1"],
            )
            self.assertEqual(
                {
                    record["accession"]
                    for record in pack["excluded_alternate_haplotype_records"]
                },
                {
                    "GCA_000000012.1",
                    "GCA_000000013.1",
                    "GCA_000000014.1",
                    "GCA_000000015.1",
                    "GCA_000000016.1",
                },
            )
            metadata_record = next(
                record
                for record in pack["excluded_alternate_haplotype_records"]
                if record["accession"] == "GCA_000000016.1"
            )
            self.assertEqual(
                metadata_record["alternate_haplotype_reason"], "assembly_metadata"
            )

    def test_alias_reverse_lookup_supplies_family_and_order_fallback_hints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [
                    self._row("GCA_000000003.1", "Otherus familymember", "2"),
                ],
                {"2": "Metazoa;Targetiformes;Targetidae;Otherus;Otherus familymember"},
            )

            pack = build_related_species_evidence_pack(
                "Targetus absentus", reference_dir=root
            )

            self.assertEqual(pack["target"]["resolution"], "alias_table")
            self.assertEqual(pack["matching_mode"], "same_family")
            self.assertEqual(pack["selected_records"][0]["taxid"], "2")

    def test_same_order_is_used_after_same_family_has_no_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [
                    self._row("GCA_000000004.1", "Otherus ordermember", "3"),
                ],
                {"3": "Metazoa;Targetiformes;Otheridae;Otherus;Otherus ordermember"},
            )

            pack = build_related_species_evidence_pack("目标鱼", reference_dir=root)

            self.assertEqual(pack["matching_mode"], "same_order")
            self.assertEqual(pack["selected_records"][0]["taxid"], "3")

    def test_blank_second_header_supports_family_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [
                    self._row("GCA_000000003.1", "Otherus familymember", "2"),
                ],
                {"2": "Metazoa;Targetiformes;Targetidae;Otherus;Otherus familymember"},
                lineage_header="",
            )

            pack = build_related_species_evidence_pack(
                "Targetus absentus", reference_dir=root
            )

            self.assertEqual(pack["matching_mode"], "same_family")
            self.assertEqual(pack["selected_records"][0]["taxid"], "2")

    def test_missing_target_is_non_blocking_and_exposes_source_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(root, [], {})

            pack = build_related_species_evidence_pack(None, reference_dir=root)

            self.assertEqual(pack["status"], "unavailable")
            self.assertEqual(pack["matching_mode"], "unavailable")
            self.assertIn("target_species_not_provided", pack["notes"])
            self.assertEqual(pack["source"]["tsv"], str(root / "Metazoa.ncbi.tsv"))

    def test_source_provenance_contains_verified_hashes_and_rejects_changed_snapshot(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [self._row("GCA_000000020.1", "Targetus relative", "20")],
                {"20": "Metazoa;Targetiformes;Targetidae;Targetus;Targetus relative"},
            )

            verified = build_related_species_evidence_pack("目标鱼", reference_dir=root)
            self.assertEqual(verified["source"]["verification"], "verified")
            self.assertEqual(verified["source"]["source_version"], "test-v1")
            self.assertEqual(
                verified["source"]["files"]["Metazoa.ncbi.tsv"]["sha256"],
                self._sha256(root / "Metazoa.ncbi.tsv"),
            )

            with (root / "Metazoa.ncbi.tsv").open("a", encoding="utf-8") as handle:
                handle.write("\n")
            mismatch = build_related_species_evidence_pack("目标鱼", reference_dir=root)
            self.assertEqual(mismatch["status"], "unavailable")
            self.assertIn(
                "reference_version_mismatch:Metazoa.ncbi.tsv", mismatch["notes"]
            )

    def test_verified_snapshot_uses_the_exact_hashed_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            original_accession = "GCA_000000020.1"
            changed_accession = "GCA_000000099.1"
            self._write_records(
                root,
                [self._row(original_accession, "Targetus absentus", "20")],
                {"20": "Metazoa;Targetiformes;Targetidae;Targetus"},
            )
            from app.reference import ncbi

            verify = ncbi._reference_provenance

            def mutate_after_verification(*args, **kwargs):
                result = verify(*args, **kwargs)
                tsv = root / "Metazoa.ncbi.tsv"
                tsv.write_text(
                    tsv.read_text(encoding="utf-8").replace(
                        original_accession, changed_accession
                    ),
                    encoding="utf-8",
                )
                return result

            with patch(
                "app.reference.ncbi._reference_provenance",
                side_effect=mutate_after_verification,
            ):
                pack = build_related_species_evidence_pack(
                    "Targetus absentus", reference_dir=root
                )

            self.assertEqual(
                pack["selected_records"][0]["accession"], original_accession
            )

    def test_assembly_review_node_writes_pack_from_manifest_user_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reference_dir = self._review_reference_dir(tmpdir)
            state = self._state(
                tmpdir,
                "session_id: s1\nuser_metadata:\n  target_species: 卵鳎\nassets: []\n",
            )
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "evaluated",
                    "related_species_reference_dir": str(reference_dir),
                }
            }

            update = build_review_evidence_pack(state)

            evidence = update["stage_outputs"]["assembly_review"][
                "related_species_evidence_pack"
            ]
            ref = Path(
                update["stage_outputs"]["assembly_review"][
                    "related_species_evidence_pack_ref"
                ]
            )
            self.assertEqual(evidence["matching_mode"], "same_genus")
            self.assertTrue(ref.exists())
            self.assertEqual(
                ref.parent,
                SessionPaths.from_run_dir(
                    state["data_dir"]
                ).workflow_step_artifacts_dir(
                    "step-002-assembly_review", "assembly_review"
                ),
            )
            self.assertEqual(
                json.loads(ref.read_text(encoding="utf-8"))["selected_records"],
                evidence["selected_records"],
            )

    def test_assembly_review_node_skips_all_failure_statuses_without_artifact(self):
        for status in ("failed", "blocked_env", "missing_tool"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmpdir:
                state = self._state(tmpdir, "session_id: s1\nassets: []\n")
                state["stage_outputs"] = {"assembly_review": {"status": status}}

                self.assertEqual(build_review_evidence_pack(state), {})
                self.assertFalse(
                    (Path(state["data_dir"]) / "artifacts" / "assembly_review").exists()
                )

    def test_review_report_and_llm_payload_receive_structured_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reference_dir = self._review_reference_dir(tmpdir)
            state = self._state(
                tmpdir,
                "session_id: s1\nuser_metadata:\n  target_species: 卵鳎\nassets: []\n",
            )
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "evaluated",
                    "related_species_reference_dir": str(reference_dir),
                    "candidates": [_candidate("candidate-1")],
                    "signals": [],
                    "review_plan": {
                        "planned_signals": ["quast"],
                        "missing_signals": ["quast"],
                    },
                },
            }
            pack_update = build_review_evidence_pack(state)
            state["stage_outputs"] = pack_update["stage_outputs"]

            with patch(
                "app.stages.assembly_review.nodes._candidate_primary_output_path",
                return_value="draft.fa",
            ):
                review_update = human_review_gate(state)

            ar_out = review_update["stage_outputs"]["assembly_review"]
            request = load_review_request(
                state["data_dir"],
                ar_out["review_request_artifact_ref"],
                session_id=state["session_id"],
            )["payload"]
            report_text = Path(ar_out["assembly_review_report_ref"]).read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                request["related_species_evidence_pack"]["matching_mode"], "same_genus"
            )
            self.assertEqual(
                request["related_species_evidence_pack"]["computed_summary"][
                    "selected_record_count"
                ],
                3,
            )
            self.assertIn("related_species_evidence_pack", request["report_refs"])
            self.assertIn("Related-species sanity check", report_text)
            self.assertIn("Solea senegalensis", report_text)
            self.assertIn("related_species_evidence_pack_ref", report_text)


if __name__ == "__main__":
    unittest.main()
