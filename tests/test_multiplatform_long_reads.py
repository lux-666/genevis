"""End-to-end provenance tests for mixed long-read assembly inputs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.domains.assembly.material_query import build_material_query
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state
from app.schemas.t2t import T2TThresholds
from app.stages.assembly.nodes import resolve_extended_inputs
from app.stages.inventory.builder import build_stage as build_inventory_stage
from app.stages.t2t.parameters.execution import (
    T2TFrozenExecutionInputs,
    T2TLongReadInput,
    load_chromosome_names_snapshot,
)
from app.storage.artifacts import resolve_artifact_ref
from app.storage.frozen_inputs import freeze_session_inputs, validate_frozen_inputs
from app.storage.paths import SessionPaths
from tests.busco_fixture import assembly_review_config, ensure_test_host_envs
from tests.data_v2 import v2_run_dir


class MultiplatformLongReadTests(unittest.TestCase):
    @staticmethod
    def _reads(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        return path

    def test_inventory_to_hifiasm_preserves_every_member_and_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            session_id = "mixed-hifiasm"
            run_dir = v2_run_dir(root / "data", session_id)
            hifi = [
                self._reads(root / "hifi-a" / "reads.fastq"),
                self._reads(root / "hifi-b" / "reads.fastq"),
            ]
            ont = [
                self._reads(root / "ont-a" / "reads.fastq"),
                self._reads(root / "ont-b" / "reads.fastq"),
            ]
            hic_r1 = [
                self._reads(root / "hic" / "L1-R1.fastq"),
                self._reads(root / "hic" / "L2-R1.fastq"),
            ]
            hic_r2 = [
                self._reads(root / "hic" / "L1-R2.fastq"),
                self._reads(root / "hic" / "L2-R2.fastq"),
            ]
            manifest = {
                "session_id": session_id,
                "assets": [
                    {
                        "asset_type": "raw_long_reads",
                        "paths": [str(path) for path in hifi],
                        "platform": "pacbio_hifi",
                        "format": "fastq",
                    },
                    {
                        "asset_type": "raw_long_reads",
                        "paths": [str(path) for path in ont],
                        "platform": "ont",
                        "format": "fastq",
                    },
                    {
                        "asset_type": "hic_reads_registered",
                        "platform": "illumina",
                        "format": "fastq",
                        "layout": "paired",
                        "paired_reads": [
                            {"r1": str(r1), "r2": str(r2)}
                            for r1, r2 in zip(hic_r1, hic_r2, strict=True)
                        ],
                    },
                ],
            }
            manifest_path = run_dir / "control" / "manifest.yaml"
            manifest_path.write_text(
                yaml.safe_dump(manifest, sort_keys=False),
                encoding="utf-8",
            )
            run_config_path = root / "run_config.json"
            run_config_path.write_text('{"requested_stages": []}\n', encoding="utf-8")
            freeze_session_inputs(
                SessionPaths.from_run_dir(run_dir),
                manifest_draft=manifest_path,
                run_config_draft=run_config_path,
                config_path=ensure_test_host_envs(root),
                selected_methods={},
            )
            state = initial_state(
                session_id=session_id,
                input_manifest_ref=SessionPaths.from_run_dir(run_dir).to_ref(
                    SessionPaths.from_run_dir(run_dir).input_manifest_path
                ),
                data_dir=str(run_dir),
                workflow_steps=[
                    {
                        "step_id": "step-000-inventory",
                        "stage": "inventory",
                        "allow_local_execution": False,
                    },
                    {
                        "step_id": "step-001-assembly",
                        "stage": "assembly",
                        "allow_local_execution": True,
                    },
                    {
                        "step_id": "step-002-assembly_review",
                        "stage": "assembly_review",
                        "allow_local_execution": False,
                        "review_target": {
                            "resource_type": "assembly",
                            "producer_step_id": "step-001-assembly",
                        },
                    },
                ],
            )
            state["entered_workflow_step_index"] = 0

            registered = build_inventory_stage(
                verified_inputs=validate_frozen_inputs(
                    SessionPaths.from_run_dir(run_dir)
                )
            ).invoke(state)
            registry = json.loads(
                resolve_artifact_ref(
                    registered["data_dir"],
                    registered["session_id"],
                    registered["asset_registry_ref"],
                ).read_text(encoding="utf-8")
            )
            registered_long_reads = [
                record
                for record in registry["assets"]
                if record["asset_type"] == "raw_long_reads"
            ]

            self.assertEqual(len(registered_long_reads), 4)
            self.assertEqual(
                len({record["asset_id"] for record in registry["assets"]}), 8
            )
            self.assertEqual(
                [record["platform"] for record in registered_long_reads],
                ["pacbio_hifi", "pacbio_hifi", "ont", "ont"],
            )
            self.assertNotIn(
                "assets",
                registered["resource_refs"]["long_reads"]["metadata"],
            )

            registered["entered_workflow_step_index"] = 1
            registered["stage_outputs"]["assembly"] = {
                "assembly_route": "hifi",
                "assembly_selection": {
                    "read_type": "pacbio_hifi",
                    "primary_method": "assembly.candidate.hifiasm",
                    "source": "run_config",
                    "rationale": "explicit mixed-input Hifiasm test",
                },
                "hifiasm_parameters": {
                    "use_ultralong_ont": True,
                    "use_hic_phasing": True,
                },
            }

            update = resolve_extended_inputs(registered)
            assembly = update["stage_outputs"]["assembly"]

            self.assertEqual(
                assembly["primary_long_read_refs"],
                [str(path.resolve()) for path in hifi],
            )
            self.assertEqual(
                assembly["ultralong_read_refs"],
                [str(path.resolve()) for path in ont],
            )
            self.assertEqual(
                assembly["hic_r1_reads"],
                [str(path.resolve()) for path in hic_r1],
            )
            self.assertEqual(
                assembly["hic_r2_reads"],
                [str(path.resolve()) for path in hic_r2],
            )
            self.assertEqual(
                {
                    role: len(ids)
                    for role, ids in assembly["assembly_input_member_ids"].items()
                },
                {"primary": 2, "ultralong": 2, "hic_r1": 2, "hic_r2": 2},
            )

            registered["stage_outputs"]["assembly"]["hifiasm_parameters"] = {}
            unphased = resolve_extended_inputs(registered)["stage_outputs"]["assembly"]
            self.assertTrue(unphased["has_hic"])
            self.assertIsNone(unphased["hic_reads_ref"])
            self.assertEqual(unphased["hic_r1_reads"], [])
            self.assertTrue(
                build_material_query(registered, unphased)["facts"]["asset_presence"][
                    "has_hic"
                ]
            )

    def test_t2t_run_config_accepts_each_supported_long_read_platform(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": [
                    {"stage": "t2t", "allow_local_execution": True},
                    {"stage": "assembly_review", "allow_local_execution": False},
                ],
                "t2t": {
                    "read_inputs": [
                        {"path": "/reads/cyclone.fastq", "platform": "cycloneseq"},
                        {"path": "/reads/ont.fastq", "platform": "ont"},
                        {"path": "/reads/clr.fastq", "platform": "clr"},
                        {"path": "/reads/hifi.fastq", "platform": "pacbio_hifi"},
                    ]
                },
                "assembly_review": assembly_review_config(
                    "/data/busco/embryophyta_odb10"
                ),
            }
        )

        self.assertEqual(
            [item.platform for item in config.t2t.read_inputs],
            ["cycloneseq", "ont", "clr", "pacbio_hifi"],
        )

        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {
                    "requested_stages": [
                        {"stage": "t2t", "allow_local_execution": True},
                        {"stage": "assembly_review", "allow_local_execution": False},
                    ],
                    "t2t": {
                        "read_inputs": [
                            {
                                "path": "/reads/unclassified.fastq",
                                "platform": "unknown",
                            }
                        ]
                    },
                    "assembly_review": assembly_review_config(
                        "/data/busco/embryophyta_odb10"
                    ),
                }
            )

    def test_t2t_frozen_inputs_keep_chromosome_names_and_platforms_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            chromosome_list = Path(tmpdir) / "chromosomes.txt"
            chromosome_list.write_text(
                "# selected chromosomes\nchr2\nchr1\nchr2\n",
                encoding="utf-8",
            )
            chromosome_names = load_chromosome_names_snapshot(str(chromosome_list))
            platforms = ("cycloneseq", "ont", "clr", "pacbio_hifi")
            frozen = T2TFrozenExecutionInputs(
                thresholds=T2TThresholds(
                    min_motif_copies=1,
                    terminal_window_bp=1000,
                    min_mapq=1,
                    max_target_terminal_distance_bp=1000,
                    min_overlap_bp=1,
                    min_supporting_reads=1,
                ),
                resolved_telomere_motif="TTAGGG",
                motif_source="provided_species_fact",
                mode="propose_only",
                input_assembly_ref="/assembly/draft.fasta",
                long_read_inputs=tuple(
                    T2TLongReadInput(
                        member_id=f"member-{platform}",
                        source_asset_id=f"asset-{platform}",
                        path=f"/reads/{platform}.fastq",
                        platform=platform,
                    )
                    for platform in platforms
                ),
                chromosome_list_ref=str(chromosome_list),
                chromosome_names=chromosome_names,
                work_dir="/work/t2t",
                execution_enabled=True,
            )

            chromosome_list.write_text("chr3\n", encoding="utf-8")

            self.assertEqual(frozen.chromosome_names, ("chr1", "chr2"))
            self.assertEqual(
                tuple(item.platform for item in frozen.long_read_inputs),
                platforms,
            )
            self.assertEqual(
                load_chromosome_names_snapshot(str(chromosome_list)),
                ("chr3",),
            )


if __name__ == "__main__":
    unittest.main()
