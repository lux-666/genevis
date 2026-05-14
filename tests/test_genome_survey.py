"""Tests for Phase 3 genome_survey realization."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.genome_survey.builder import build_stage


class GenomeSurveyDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_genome_survey_placeholders(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        for package_name in ["jellyfish", "meryl", "genomescope2", "smudgeplot"]:
            self.assertIn(package_name, registry.packages)

        expected_methods = {
            "genome_survey.kmer_count.jellyfish",
            "genome_survey.kmer_count.meryl",
            "genome_survey.model.genomescope2",
            "genome_survey.model.smudgeplot",
        }
        self.assertTrue(expected_methods.issubset(set(registry.by_stage["genome_survey"])))


class GenomeSurveyStageTests(unittest.TestCase):
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
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
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
        genomescope = bin_dir / "genomescope.R"
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
                "outdir=\"$4\"\n"
                "mkdir -p \"$outdir\"\n"
                f"printf '{summary}' > \"$outdir/summary.tsv\"\n",
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

    def test_full_mode_with_clean_short_reads_writes_high_confidence_plan(self):
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

            result = build_stage().invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["mode"], "full")
            self.assertEqual(survey["confidence"], "high")
            self.assertEqual(survey["skip_reason"], None)
            self.assertIn("genome_survey.kmer_count.jellyfish", survey["candidate_methods"])
            self.assertIn("genome_survey.model.genomescope2", survey["candidate_methods"])
            artifact_ref = Path(survey["result_artifact_ref"])
            self.assertTrue(artifact_ref.exists())
            payload = json.loads(artifact_ref.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "full")
            self.assertEqual(payload["confidence"], "high")

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
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "survey_parameters": {
                        "kmer_size": 21,
                        "read_length": 150,
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
                for line in (Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("genome_survey.kmer_db", artifact_types)
            self.assertIn("genome_survey.kmer_hist", artifact_types)
            self.assertIn("genome_survey.model_summary", artifact_types)
            self.assertIn("genome_survey_result", artifact_types)

            ledger_events = [
                json.loads(line)
                for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertEqual(event_types.count("attempt_started"), 2)
            self.assertEqual(event_types.count("attempt_succeeded"), 2)
            self.assertIn("artifact_registered", event_types)

    def test_local_full_mode_keeps_unparseable_genomescope_summary_nonfatal(self):
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
                        "read_length": 150,
                        "hash_size": "10M",
                    },
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "completed")
            self.assertEqual(
                survey["estimates"],
                {"genome_size": None, "heterozygosity": None, "repeat_fraction": None},
            )
            self.assertIn("genomescope2_genome_size_unparsed", survey["notes"])
            self.assertIn("genomescope2_heterozygosity_unparsed", survey["notes"])
            self.assertIn("genomescope2_repeat_fraction_unparsed", survey["notes"])
            self.assertIn("genomescope2_estimates_unparsed", survey["notes"])

    def test_local_mode_with_long_reads_only_does_not_run_kmer_tools(self):
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
            called_marker = Path(tmpdir) / "called"
            fake_bin = self._fake_survey_bin(tmpdir, fail_if_called=True)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "execution_mode": "local",
                    "env": {
                        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                        "GENOME_SURVEY_CALLED": str(called_marker),
                    },
                    "survey_parameters": {"kmer_size": 21, "read_length": 150},
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["mode"], "fallback_low_confidence")
            self.assertEqual(survey["confidence"], "low")
            self.assertEqual(survey["candidate_methods"], [])
            self.assertFalse(called_marker.exists())
            self.assertNotIn("kmer_hist_artifact_ref", survey)
            ledger_events = [
                json.loads(line)
                for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertNotIn("attempt_started", event_types)
            self.assertNotIn("attempt_succeeded", event_types)

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
                    "survey_parameters": {"kmer_size": 21, "read_length": 150},
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "genome_survey")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("survey_parameters.hash_size", result["last_error"])
            self.assertFalse((Path(tmpdir) / "ledger" / "s1" / "events.jsonl").exists())

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
            self.assertFalse((Path(tmpdir) / "ledger" / "s1" / "events.jsonl").exists())

    def test_long_read_only_fallback_is_low_confidence(self):
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
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "completed",
                    "summary": {"total_bases": 10_000_000},
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["mode"], "fallback_low_confidence")
            self.assertEqual(survey["confidence"], "low")
            self.assertNotEqual(survey["confidence"], "high")
            self.assertEqual(survey["estimates"]["coverage"], 5.0)
            self.assertEqual(survey["candidate_methods"], [])
            self.assertIn("not_equivalent_to_kmer_survey", survey["notes"])
            payload = json.loads(Path(survey["result_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["candidate_methods"], [])
            self.assertIn("not_equivalent_to_kmer_survey", payload["notes"])

    def test_no_usable_inputs_writes_skip_record(self):
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

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "skipped")
            self.assertEqual(survey["mode"], "skipped")
            self.assertEqual(survey["skip_reason"], "no_low_error_short_reads")
            self.assertEqual(result["skip_reasons"]["genome_survey"], "no_low_error_short_reads")
            self.assertTrue(Path(survey["result_artifact_ref"]).exists())


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
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
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
                        "read_length": 150,
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
                for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text().splitlines()
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
                        "read_length": 150,
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
            #   - reports `jellyfish` present but `genomescope.R` missing
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
                "    if [ \"$5\" = \"genomescope.R\" ]; then exit 1; fi\n"
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
                        "read_length": 150,
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
            self.assertIn("genomescope.R", survey["missing_tools"])


if __name__ == "__main__":
    unittest.main()
