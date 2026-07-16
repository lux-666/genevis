"""Tests for Phase 3 genome_survey realization."""

from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.genome_survey.builder import build_stage
from app.stages.genome_survey.method_application.genomescope2 import build_genomescope2_graph
from app.stages.genome_survey.method_application.jellyfish import build_jellyfish_graph
from app.stages.genome_survey.nodes import _parse_genomescope_summary
from tests.data_v2 import v2_run_dir


class GenomeSurveyDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_genome_survey_placeholders(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        for package_name in ["jellyfish", "genomescope2"]:
            self.assertIn(package_name, registry.packages)

        expected_methods = {
            "genome_survey.kmer_count.jellyfish",
            "genome_survey.model.genomescope2",
        }
        self.assertTrue(expected_methods.issubset(set(registry.by_stage["genome_survey"])))


class GenomeSurveyStageTests(unittest.TestCase):
    def test_method_modules_do_not_depend_on_parent_stage_modules(self):
        method_modules = [
            Path("app/stages/genome_survey/method_application/jellyfish.py"),
            Path("app/stages/genome_survey/method_application/genomescope2.py"),
        ]
        forbidden = {
            "app.stages.genome_survey.nodes",
            "app.stages.genome_survey.routing",
        }

        for module_path in method_modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            imported_modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module is not None:
                        imported_modules.add(node.module)
                    if node.module == "app.stages.genome_survey":
                        imported_modules.update(
                            f"app.stages.genome_survey.{alias.name}" for alias in node.names
                        )
            self.assertTrue(
                forbidden.isdisjoint(imported_modules),
                f"{module_path} must not depend on parent stage modules",
            )

    def test_outer_graph_routes_to_method_subgraphs(self):
        outer_nodes = set(build_stage().get_graph().nodes)
        jellyfish_nodes = set(build_jellyfish_graph().get_graph().nodes)
        genomescope2_nodes = set(build_genomescope2_graph().get_graph().nodes)

        self.assertIn("jellyfish", outer_nodes)
        self.assertIn("genomescope2", outer_nodes)
        self.assertNotIn("freeze_jellyfish_attempt", outer_nodes)
        self.assertNotIn("run_genomescope2_attempt", outer_nodes)
        for nodes in [jellyfish_nodes, genomescope2_nodes]:
            self.assertIn("freeze", nodes)
            self.assertIn("run", nodes)
            self.assertIn("collect", nodes)

    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "s1", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _state(self, tmpdir: str, registry_ref: str | None):
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(tmpdir, "s1")),
        )
        state["current_stage"] = "genome_survey"
        state["asset_registry_ref"] = registry_ref
        return state

    def _fake_survey_bin(
        self,
        tmpdir: str,
        *,
        fail_if_called: bool = False,
        unparseable_summary: bool = False,
    ) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir()
        jellyfish = bin_dir / "jellyfish"
        genomescope = bin_dir / "genomescope2"
        if fail_if_called:
            script = "#!/bin/sh\necho called > \"$GENOME_SURVEY_CALLED\"\nexit 99\n"
            jellyfish.write_text(script, encoding="utf-8")
            genomescope.write_text(script, encoding="utf-8")
        else:
            jellyfish.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"count\" ]; then\n"
                "  while [ \"$1\" != \"-o\" ]; do shift; done\n"
                "  shift\n"
                "  printf 'fake-jf\\n' > \"$1\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = \"histo\" ]; then\n"
                "  printf '1\\t10\\n2\\t5\\n'\n"
                "  exit 0\n"
                "fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            summary = (
                "genome_size\\tNA\\nheterozygosity\\t0.001-0.002\\nrepeat_fraction\\tNA\\n"
                if unparseable_summary
                else "genome_size\\t12345\\nheterozygosity\\t0.001\\nrepeat_fraction\\t0.2\\n"
            )
            genomescope.write_text(
                "#!/bin/sh\n"
                "while [ \"$1\" != \"-o\" ]; do shift; done\n"
                "shift\n"
                "outdir=\"$1\"\n"
                "mkdir -p \"$outdir\"\n"
                f"printf '{summary}' > \"$outdir/summary.txt\"\n",
                encoding="utf-8",
            )
        jellyfish.chmod(0o755)
        genomescope.chmod(0o755)
        return bin_dir

    def test_missing_asset_registry_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = build_stage().invoke(self._state(tmpdir, None))

            self.assertEqual(result["current_stage"], "genome_survey")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("asset_registry_ref", result["last_error"])

    def test_explicit_size_does_not_skip_survey_when_clean_short_reads_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_r1 = Path(tmpdir) / "short_R1.fq.gz"
            short_r2 = Path(tmpdir) / "short_R2.fq.gz"
            long_reads = Path(tmpdir) / "BGI_CycloneSEQ.fastq.gz"
            for path in [short_r1, short_r2, long_reads]:
                path.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")

            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_short_dna_reads:short_R1.fq.gz",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_r1),
                        "platform": "illumina",
                    },
                    {
                        "asset_id": "s1:clean_short_dna_reads:short_R2.fq.gz",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_r2),
                        "platform": "illumina",
                    },
                    {
                        "asset_id": "s1:raw_long_reads:BGI_CycloneSEQ.fastq.gz",
                        "asset_type": "raw_long_reads",
                        "path": str(long_reads),
                        "platform": "cycloneseq",
                    },
                ],
            )

            state = self._state(tmpdir, str(registry))
            state["shared_parameters"] = {"estimated_genome_size": 2_000_000}

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["mode"], "full")
            self.assertEqual(survey["confidence"], "high")
            self.assertEqual(survey["survey_input_family"], "clean_short_dna_reads")
            self.assertEqual(survey["genome_size_prior"], 2_000_000)
            self.assertEqual(survey["genome_size_source"], "provided")
            self.assertIsNone(survey["skip_reason"])
            self.assertEqual(survey["estimates"]["genome_size"], 2_000_000)
            self.assertIsNone(survey["measured_estimates"]["genome_size"])
            self.assertEqual(survey["provided_overrides"], {"genome_size": 2_000_000})
            self.assertEqual(survey["estimate_sources"]["genome_size"], "provided")
            artifact_ref = Path(survey["result_artifact_ref"])
            self.assertTrue(artifact_ref.exists())
            payload = json.loads(artifact_ref.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "full")
            self.assertEqual(payload["status"], "planned")
            self.assertEqual(payload["genome_size_source"], "provided")

    def test_manifest_inventory_size_overrides_planned_survey_when_raw_short_reads_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_r1 = Path(tmpdir) / "short_R1.fq.gz"
            short_r2 = Path(tmpdir) / "short_R2.fq.gz"
            for path in [short_r1, short_r2]:
                path.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")

            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_short_dna_reads:short_R1.fq.gz",
                        "asset_type": "raw_short_dna_reads",
                        "path": str(short_r1),
                        "platform": "illumina",
                    },
                    {
                        "asset_id": "s1:raw_short_dna_reads:short_R2.fq.gz",
                        "asset_type": "raw_short_dna_reads",
                        "path": str(short_r2),
                        "platform": "illumina",
                    },
                ],
            )

            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {"inventory": {"genome_size_estimate": 2_000_000}}

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["mode"], "full")
            self.assertEqual(survey["confidence"], "high")
            self.assertEqual(survey["survey_input_family"], "raw_short_dna_reads")
            self.assertEqual(survey["genome_size_prior"], 2_000_000)
            self.assertEqual(survey["genome_size_source"], "provided")
            self.assertEqual(survey["genome_size_source_path"], "inventory.genome_size_estimate")
            self.assertEqual(survey["measured_estimates"]["genome_size"], None)
            self.assertEqual(
                survey["estimate_source_paths"]["genome_size"],
                "inventory.genome_size_estimate",
            )
            payload = json.loads(Path(survey["result_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["genome_size_prior"], 2_000_000)

    def test_local_full_mode_runs_jellyfish_and_genomescope2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_r1 = Path(tmpdir) / "short_R1.fq"
            short_r2 = Path(tmpdir) / "short_R2.fq"
            for path in [short_r1, short_r2]:
                path.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_short_dna_reads:short_R1.fq",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_r1),
                        "platform": "illumina",
                    },
                    {
                        "asset_id": "s1:clean_short_dna_reads:short_R2.fq",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_r2),
                        "platform": "illumina",
                    },
                ],
            )
            fake_bin = self._fake_survey_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "summary": {
                        "avg_len": 150.2,
                    },
                },
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "survey_parameters": {
                        "kmer_size": 21,
                        "hash_size": "10M",
                        "threads": 1,
                    },
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["mode"], "full")
            self.assertEqual(survey["status"], "completed")
            self.assertEqual(survey["estimates"]["genome_size"], 12345)
            self.assertEqual(survey["genome_size_prior"], 12345)

    def test_local_full_mode_accepts_raw_short_reads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_reads = Path(tmpdir) / "short.fq"
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_short_dna_reads:short.fq",
                        "asset_type": "raw_short_dna_reads",
                        "path": str(short_reads),
                        "platform": "illumina",
                    },
                ],
            )
            fake_bin = self._fake_survey_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "survey_parameters": {
                        "kmer_size": 21,
                        "hash_size": "10M",
                        "threads": 1,
                    },
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "completed")
            self.assertEqual(survey["survey_input_family"], "raw_short_dna_reads")
            self.assertEqual(survey["survey_input_refs"], ["s1:raw_short_dna_reads:short.fq"])
            payload = json.loads(Path(survey["result_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["input_asset_refs"], ["s1:raw_short_dna_reads:short.fq"])
            self.assertEqual(payload["metadata"]["input_family"], "raw_short_dna_reads")
            self.assertEqual(survey["estimates"]["heterozygosity"], 0.001)
            self.assertEqual(survey["estimates"]["repeat_fraction"], 0.2)
            self.assertTrue(Path(survey["kmer_hist_artifact_ref"]).exists())
            self.assertTrue(Path(survey["genomescope2_summary_ref"]).exists())
            self.assertTrue(Path(survey["result_artifact_ref"]).exists())
            self.assertIn("executed_jellyfish_and_genomescope2", survey["notes"])
            self.assertEqual(survey["substeps"]["kmer_count"]["status"], "executed")
            self.assertEqual(survey["substeps"]["genome_model"]["status"], "executed")
            self.assertNotIn("resolved_assets", survey)
            self.assertNotIn("survey_parameters", survey)
            self.assertNotIn("jellyfish_frozen_attempt", survey)
            self.assertNotIn("genomescope2_execution_result", survey)

            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("genome_survey.kmer_db", artifact_types)
            self.assertIn("genome_survey.kmer_hist", artifact_types)
            self.assertIn("genome_survey.model_summary", artifact_types)
            self.assertIn("genome_survey_result", artifact_types)

            ledger_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertEqual(event_types.count("attempt_started"), 2)
            self.assertEqual(event_types.count("attempt_succeeded"), 2)
            self.assertIn("artifact_registered", event_types)

    def test_local_full_mode_fails_when_genomescope_summary_has_no_genome_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_reads = Path(tmpdir) / "short.fq"
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_short_dna_reads:short.fq",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_reads),
                        "platform": "illumina",
                    }
                ],
            )
            fake_bin = self._fake_survey_bin(tmpdir, unparseable_summary=True)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "survey_parameters": {
                        "kmer_size": 21,
                        "hash_size": "10M",
                    },
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "genome_survey")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertEqual(result["last_error"], "genomescope2_genome_size_unavailable")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "failed")
            self.assertEqual(survey["error_code"], "GENOME_SIZE_UNAVAILABLE")
            self.assertIsNone(survey["genome_size_prior"])
            self.assertEqual(
                survey["estimates"],
                {"genome_size": None, "heterozygosity": None, "repeat_fraction": None},
            )
            self.assertIn("genomescope2_genome_size_unparsed", survey["notes"])
            self.assertIn("genomescope2_heterozygosity_unparsed", survey["notes"])
            self.assertIn("genomescope2_repeat_fraction_unparsed", survey["notes"])
            self.assertIn("genomescope2_estimates_unparsed", survey["notes"])

    def test_explicit_size_overrides_unparseable_genome_size_after_local_survey_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_reads = Path(tmpdir) / "short.fq"
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_short_dna_reads:short.fq",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_reads),
                        "platform": "illumina",
                    }
                ],
            )
            fake_bin = self._fake_survey_bin(tmpdir, unparseable_summary=True)
            state = self._state(tmpdir, str(registry))
            state["shared_parameters"] = {"estimated_genome_size": 2_000_000}
            state["stage_outputs"] = {
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "survey_parameters": {
                        "kmer_size": 21,
                        "hash_size": "10M",
                        "threads": 1,
                    },
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "completed")
            self.assertEqual(survey["mode"], "full")
            self.assertEqual(survey["genome_size_prior"], 2_000_000)
            self.assertEqual(survey["genome_size_source"], "provided")
            self.assertEqual(
                survey["measured_estimates"],
                {"genome_size": None, "heterozygosity": None, "repeat_fraction": None},
            )
            self.assertEqual(survey["estimates"]["genome_size"], 2_000_000)
            self.assertEqual(survey["provided_overrides"], {"genome_size": 2_000_000})
            self.assertTrue((Path(result["data_dir"]) / "work" / "genome_survey").exists())

    def test_parse_genomescope2_summary_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = Path(tmpdir) / "summary.txt"
            summary.write_text(
                "property\tmin\tmax\n"
                "Genome Haploid Length\t12,345\t12,300\t12,400\n"
                "Genome Repeat Length\t2,469\t2,400\t2,500\n"
                "Heterozygosity\t0.001\t0.0009\t0.0011\n",
                encoding="utf-8",
            )

            estimates, notes = _parse_genomescope_summary(summary)

            self.assertEqual(estimates["genome_size"], 12345)
            self.assertEqual(estimates["heterozygosity"], 0.001)
            self.assertEqual(estimates["repeat_fraction"], 2469 / 12345)
            self.assertEqual(notes, [])

    def test_parse_genomescope2_summary_table_with_bp_units(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = Path(tmpdir) / "summary.txt"
            summary.write_text(
                "property                      min               max\n"
                "Genome Haploid Length         108,518 bp        113,750 bp\n"
                "Genome Repeat Length          88,419 bp         92,682 bp\n",
                encoding="utf-8",
            )

            estimates, notes = _parse_genomescope_summary(summary)

            self.assertEqual(estimates["genome_size"], 108518)
            self.assertEqual(estimates["repeat_fraction"], 88419 / 108518)
            self.assertEqual(notes, [])

    def test_local_mode_with_long_reads_only_runs_jellyfish_and_genomescope2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            long_reads = Path(tmpdir) / "reads.fastq"
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:reads.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(long_reads),
                        "platform": "ont",
                    }
                ],
            )
            fake_bin = self._fake_survey_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "survey_parameters": {"kmer_size": 21, "hash_size": "10M", "threads": 1},
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "completed")
            self.assertEqual(survey["survey_input_family"], "raw_long_reads")
            self.assertEqual(survey["survey_input_refs"], ["s1:raw_long_reads:reads.fastq"])
            self.assertEqual(survey["estimates"]["genome_size"], 12345)
            self.assertEqual(survey["genome_size_prior"], 12345)
            self.assertEqual(survey["genome_size_source"], "survey")
            self.assertTrue(Path(survey["kmer_hist_artifact_ref"]).exists())
            self.assertTrue(Path(survey["genomescope2_summary_ref"]).exists())

    def test_prefers_read_qc_filtered_long_reads_over_raw_registry(self):
        """Survey should consume post-QC long reads like assembly, not raw manifest paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_reads = Path(tmpdir) / "raw.fastq.gz"
            raw_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            filtered = Path(tmpdir) / "filtered_long_reads.fastq"
            filtered.write_text("@r1\nACGTACGT\n+\n!!!!!!!!\n", encoding="utf-8")
            fofn = Path(tmpdir) / "filtered_long_reads.fofn"
            fofn.write_text(f"{filtered}\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:raw.fastq.gz",
                        "asset_type": "raw_long_reads",
                        "path": str(raw_reads),
                        "platform": "ont",
                    }
                ],
            )
            fake_bin = self._fake_survey_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "completed",
                    "platform": "ont",
                    "format": "fastq",
                    "filtered_long_reads_ref": str(filtered),
                    "filtered_long_reads_refs": [str(filtered)],
                    "filtered_long_reads_fofn_ref": str(fofn),
                    "assembly_input_ref": str(fofn),
                },
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "survey_parameters": {"kmer_size": 21, "hash_size": "10M", "threads": 1},
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "completed")
            self.assertEqual(survey["survey_input_family"], "clean_long_reads")
            self.assertEqual(
                survey["survey_input_refs"],
                [f"s1:clean_long_reads:read_qc:{filtered.name}"],
            )
            # Jellyfish must have been pointed at filtered path, not raw gz.
            script = Path(result["data_dir"]) / "work" / "genome_survey" / "jellyfish" / "run_jellyfish.sh"
            self.assertTrue(script.exists(), "expected jellyfish script under work/")
            script_text = script.read_text(encoding="utf-8")
            self.assertIn(str(filtered), script_text)
            self.assertNotIn(str(raw_reads), script_text)

    def test_read_qc_declared_missing_paths_fail_without_raw_fallback(self):
        """Declared QC long-read refs must not silently fall back to raw registry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_reads = Path(tmpdir) / "raw.fastq.gz"
            raw_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            missing = Path(tmpdir) / "missing_filtered.fastq"
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:raw.fastq.gz",
                        "asset_type": "raw_long_reads",
                        "path": str(raw_reads),
                        "platform": "ont",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "completed",
                    "platform": "ont",
                    "format": "fastq",
                    "filtered_long_reads_ref": str(missing),
                    "filtered_long_reads_refs": [str(missing)],
                    "assembly_input_ref": str(missing),
                },
                "genome_survey": {
                    "execution_mode": "local",
                    "survey_parameters": {"kmer_size": 21, "hash_size": "10M", "threads": 1},
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "failed")
            self.assertIn("missing on disk", survey["reason"])
            self.assertIn("refusing to fall back", survey["reason"])

    def test_read_qc_fofn_relative_paths_resolve_against_fofn_dir(self):
        """Relative entries in a QC FOFN resolve relative to the FOFN file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_reads = Path(tmpdir) / "raw.fastq.gz"
            raw_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            qc_dir = Path(tmpdir) / "qc_out"
            qc_dir.mkdir()
            filtered = qc_dir / "filtered_long_reads.fastq"
            filtered.write_text("@r1\nACGTACGT\n+\n!!!!!!!!\n", encoding="utf-8")
            fofn = qc_dir / "filtered_long_reads.fofn"
            fofn.write_text("filtered_long_reads.fastq\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:raw.fastq.gz",
                        "asset_type": "raw_long_reads",
                        "path": str(raw_reads),
                        "platform": "ont",
                    }
                ],
            )
            fake_bin = self._fake_survey_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "fofn_ready",
                    "platform": "ont",
                    "format": "fastq",
                    "filtered_long_reads_fofn_ref": str(fofn),
                    "assembly_input_ref": str(fofn),
                },
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "survey_parameters": {"kmer_size": 21, "hash_size": "10M", "threads": 1},
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "completed")
            self.assertEqual(survey["survey_input_family"], "clean_long_reads")
            script = Path(result["data_dir"]) / "work" / "genome_survey" / "jellyfish" / "run_jellyfish.sh"
            script_text = script.read_text(encoding="utf-8")
            self.assertIn(str(filtered.resolve()), script_text)
            self.assertNotIn(str(raw_reads), script_text)

    def test_short_dna_still_preferred_over_read_qc_long(self):
        """Short DNA family selection remains higher priority than QC long reads."""
        with tempfile.TemporaryDirectory() as tmpdir:
            short_reads = Path(tmpdir) / "short.fq"
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            filtered = Path(tmpdir) / "filtered_long_reads.fastq"
            filtered.write_text("@r1\nACGTACGT\n+\n!!!!!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_short_dna_reads:short.fq",
                        "asset_type": "raw_short_dna_reads",
                        "path": str(short_reads),
                    }
                ],
            )
            fake_bin = self._fake_survey_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "completed",
                    "platform": "ont",
                    "format": "fastq",
                    "filtered_long_reads_ref": str(filtered),
                    "filtered_long_reads_refs": [str(filtered)],
                },
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "survey_parameters": {"kmer_size": 21, "hash_size": "10M", "threads": 1},
                },
            }

            result = build_stage().invoke(state)

            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "completed")
            self.assertEqual(survey["survey_input_family"], "raw_short_dna_reads")
            script = Path(result["data_dir"]) / "work" / "genome_survey" / "jellyfish" / "run_jellyfish.sh"
            script_text = script.read_text(encoding="utf-8")
            self.assertIn(str(short_reads), script_text)
            self.assertNotIn(str(filtered), script_text)

    def test_jellyfish_freeze_rejects_empty_input_paths(self):
        """Local jellyfish freeze must fail closed when resolved assets are empty."""
        from app.stages.genome_survey.method_application.jellyfish import freeze_jellyfish_attempt

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, str(self._write_registry(tmpdir, assets=[])))
            state["stage_outputs"] = {
                "genome_survey": {
                    "status": "local_execution_planned",
                    "execution_mode": "local",
                    "survey_input_family": "clean_long_reads",
                    "survey_input_refs": [],
                    "resolved_assets": {"clean_long_reads": []},
                    "survey_parameters": {"kmer_size": 21, "hash_size": "10M", "threads": 1},
                    "env_target": "local",
                }
            }

            update = freeze_jellyfish_attempt(state)
            survey = update["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "failed")
            self.assertIn("no readable input paths", survey["reason"])

    def test_local_full_mode_requires_explicit_hash_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_reads = Path(tmpdir) / "short.fq"
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_short_dna_reads:short.fq",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_reads),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "execution_mode": "local",
                    "survey_parameters": {"kmer_size": 21},
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "genome_survey")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("survey_parameters.hash_size", result["last_error"])
            self.assertFalse((Path(state["data_dir"]) / "ledger" / "events.jsonl").exists())

    def test_local_full_mode_requires_explicit_survey_parameters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_reads = Path(tmpdir) / "short.fq"
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_short_dna_reads:short.fq",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_reads),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {"genome_survey": {"execution_mode": "local"}}

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "genome_survey")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("survey_parameters.kmer_size", result["last_error"])
            self.assertFalse((Path(state["data_dir"]) / "ledger" / "events.jsonl").exists())

    def test_explicit_genome_size_overrides_measurement_but_keeps_other_survey_estimates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            long_reads = Path(tmpdir) / "BGI_CycloneSEQ.fastq.gz"
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:BGI_CycloneSEQ.fastq.gz",
                        "asset_type": "raw_long_reads",
                        "path": str(long_reads),
                        "platform": "cycloneseq",
                        "metadata": {"genome_size_estimate": 2_000_000},
                    }
                ],
            )
            fake_bin = self._fake_survey_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "survey_parameters": {"kmer_size": 21, "hash_size": "10M", "threads": 1},
                }
            }
            state["shared_parameters"] = {"estimated_genome_size": 2_000_000}

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["mode"], "full")
            self.assertEqual(survey["status"], "completed")
            self.assertEqual(survey["measured_estimates"]["genome_size"], 12345)
            self.assertEqual(survey["estimates"]["genome_size"], 2_000_000)
            self.assertEqual(survey["estimates"]["heterozygosity"], 0.001)
            self.assertEqual(survey["estimates"]["repeat_fraction"], 0.2)
            self.assertEqual(survey["provided_overrides"], {"genome_size": 2_000_000})
            self.assertEqual(
                survey["estimate_sources"],
                {
                    "genome_size": "provided",
                    "heterozygosity": "survey",
                    "repeat_fraction": "survey",
                },
            )
            self.assertEqual(survey["genome_size_prior"], 2_000_000)
            self.assertEqual(survey["genome_size_source"], "provided")
            payload = json.loads(Path(survey["result_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["measured_estimates"]["genome_size"], 12345)
            self.assertEqual(payload["estimates"]["genome_size"], 2_000_000)
            self.assertEqual(payload["genome_size_source"], "provided")

    def test_no_usable_inputs_fails_with_stable_genome_size_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rnaseq = Path(tmpdir) / "rna_R1.fq.gz"
            rnaseq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_rnaseq_reads:rna_R1.fq.gz",
                        "asset_type": "raw_rnaseq_reads",
                        "path": str(rnaseq),
                    }
                ],
            )

            result = build_stage().invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "genome_survey")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "failed")
            self.assertEqual(survey["error_code"], "GENOME_SIZE_UNAVAILABLE")
            self.assertIsNone(survey["genome_size_prior"])

    def test_no_usable_inputs_falls_back_to_explicit_genome_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rnaseq = Path(tmpdir) / "rna_R1.fq.gz"
            rnaseq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_rnaseq_reads:rna_R1.fq.gz",
                        "asset_type": "raw_rnaseq_reads",
                        "path": str(rnaseq),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["shared_parameters"] = {"estimated_genome_size": 2_000_000}

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["mode"], "skipped")
            self.assertEqual(survey["status"], "skipped")
            self.assertEqual(survey["skip_reason"], "no_usable_survey_reads")
            self.assertEqual(survey["genome_size_prior"], 2_000_000)
            self.assertEqual(survey["estimates"], {"genome_size": 2_000_000})
            self.assertEqual(survey["provided_overrides"], {"genome_size": 2_000_000})


class GenomeSurveyEnvPreflightTests(unittest.TestCase):
    """Phase 5 task 01: jellyfish/genomescope2 must run through env_dispatch."""

    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "s1", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _state(self, tmpdir: str, registry_ref: str):
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(tmpdir, "s1")),
        )
        state["current_stage"] = "genome_survey"
        state["asset_registry_ref"] = registry_ref
        return state

    def _short_read_registry(self, tmpdir: str) -> Path:
        short_reads = Path(tmpdir) / "short.fq"
        short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        return self._write_registry(
            tmpdir,
            assets=[
                {
                    "asset_id": "s1:clean_short_dna_reads:short.fq",
                    "asset_type": "clean_short_dna_reads",
                    "path": str(short_reads),
                    "platform": "illumina",
                }
            ],
        )

    def test_missing_conda_env_blocks_jellyfish_with_blocked_env_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._short_read_registry(tmpdir)
            state = self._state(tmpdir, str(registry))
            # No env.PATH override → env_dispatch resolves and preflights via
            # config/envs.yaml. Empty PATH removes conda so preflight reports
            # blocked_env before any jellyfish/genomescope2 command runs.
            state["stage_outputs"] = {
                "genome_survey": {
                    "execution_mode": "local",
                    "survey_parameters": {
                        "kmer_size": 21,
                        "hash_size": "10M",
                    },
                }
            }
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ""
            try:
                result = build_stage().invoke(state)
            finally:
                os.environ["PATH"] = old_path

            self.assertEqual(result["current_stage"], "genome_survey")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "blocked_env")
            self.assertEqual(survey["method_name"], "genome_survey.kmer_count.jellyfish")
            self.assertIn("conda", result["last_error"])
            ledger_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertIn("env_preflight_blocked", event_types)
            self.assertNotIn("attempt_started", event_types)

    def test_missing_jellyfish_in_env_blocks_with_missing_tool_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._short_read_registry(tmpdir)
            # Stub conda that reports env ok but every tool probe fails, so
            # the first method (jellyfish) blocks before any binary runs.
            conda_stub_dir = Path(tmpdir) / "conda-stub-bin"
            conda_stub_dir.mkdir()
            conda_stub = conda_stub_dir / "conda"
            conda_stub.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"run\" ]; then\n"
                "  shift\n"
                "  while [ \"$#\" -gt 0 ]; do\n"
                "    case \"$1\" in\n"
                "      --live-stream|--no-capture-output) shift ;;\n"
                "      -n|-p) shift 2 ;;\n"
                "      *) break ;;\n"
                "    esac\n"
                "  done\n"
                "  if [ \"$1\" = \"true\" ]; then exit 0; fi\n"
                "  exit 1\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            conda_stub.chmod(0o755)

            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "execution_mode": "local",
                    "survey_parameters": {
                        "kmer_size": 21,
                        "hash_size": "10M",
                    },
                }
            }
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{conda_stub_dir}{os.pathsep}{old_path}"
            try:
                result = build_stage().invoke(state)
            finally:
                os.environ["PATH"] = old_path

            self.assertEqual(result["supervision_state"], "failed_terminal")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "missing_tool")
            self.assertEqual(survey["method_name"], "genome_survey.kmer_count.jellyfish")
            self.assertIn("jellyfish", survey["missing_tools"])

    def test_missing_genomescope_after_jellyfish_succeeds_blocks_with_missing_tool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_reads = Path(tmpdir) / "short.fq"
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_short_dna_reads:short.fq",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_reads),
                        "platform": "illumina",
                    }
                ],
            )
            # Stub conda that:
            #   - reports env ok for the env probe (`true`)
            #   - reports `jellyfish` present but `genomescope2` missing
            #   - executes jellyfish itself by writing the expected outputs
            conda_stub_dir = Path(tmpdir) / "conda-stub-bin"
            conda_stub_dir.mkdir()
            conda_stub = conda_stub_dir / "conda"
            conda_stub.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"run\" ]; then\n"
                "  shift\n"
                "  while [ \"$#\" -gt 0 ]; do\n"
                "    case \"$1\" in\n"
                "      --live-stream|--no-capture-output) shift ;;\n"
                "      -n|-p) shift 2 ;;\n"
                "      *) break ;;\n"
                "    esac\n"
                "  done\n"
                "  if [ \"$1\" = \"true\" ]; then exit 0; fi\n"
                "  if [ \"$1\" = \"sh\" ] && [ \"$2\" = \"-c\" ]; then\n"
                "    # Tool probe: positional $1 is the tool name (5th arg of `sh -c ... preflight TOOL`).\n"
                "    if [ \"$5\" = \"jellyfish\" ]; then exit 0; fi\n"
                "    if [ \"$5\" = \"genomescope2\" ]; then exit 1; fi\n"
                "    exit 1\n"
                "  fi\n"
                "  if [ \"$1\" = \"/bin/bash\" ]; then\n"
                "    # Execute the script normally so jellyfish counts/histos run.\n"
                "    exec \"$@\"\n"
                "  fi\n"
                "  exit 1\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            conda_stub.chmod(0o755)
            # Provide a fake jellyfish on PATH because conda passes execution
            # through to bash, which then needs jellyfish itself.
            jelly_stub_dir = Path(tmpdir) / "jelly-stub-bin"
            jelly_stub_dir.mkdir()
            jellyfish_stub = jelly_stub_dir / "jellyfish"
            jellyfish_stub.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"count\" ]; then\n"
                "  while [ \"$1\" != \"-o\" ]; do shift; done\n"
                "  shift\n"
                "  printf 'fake-jf\\n' > \"$1\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = \"histo\" ]; then\n"
                "  printf '1\\t10\\n2\\t5\\n'\n"
                "  exit 0\n"
                "fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            jellyfish_stub.chmod(0o755)

            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "execution_mode": "local",
                    "survey_parameters": {
                        "kmer_size": 21,
                        "hash_size": "10M",
                    },
                }
            }
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = (
                f"{conda_stub_dir}{os.pathsep}{jelly_stub_dir}{os.pathsep}{old_path}"
            )
            try:
                result = build_stage().invoke(state)
            finally:
                os.environ["PATH"] = old_path

            self.assertEqual(result["supervision_state"], "failed_terminal")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "missing_tool")
            self.assertEqual(survey["method_name"], "genome_survey.model.genomescope2")
            self.assertIn("genomescope2", survey["missing_tools"])


if __name__ == "__main__":
    unittest.main()
