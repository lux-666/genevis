"""Tests for Phase 3 assembly realization."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import inspect
from pathlib import Path

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.shared_parameters import PLATFORM_TO_SHARED_READ_TYPE, SHARED_TO_METHOD_READ_TYPE
from app.schemas.state import initial_state
from app.control.lifecycle import resume_review_state
from app.review.base import ResumeReviewRequest
from app.stages.assembly.builder import build_stage
from app.stages.assembly.method_application import flye as flye_graph
from app.stages.assembly.method_application import hifiasm as hifiasm_graph
from app.stages.assembly.method_application import nextdenovo as nextdenovo_graph
from app.stages.assembly.method_application.flye import build_flye_graph
from app.stages.assembly.method_application.hifiasm import build_hifiasm_graph
from app.stages.assembly.method_application.nextdenovo import build_nextdenovo_graph
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


def _ref_path(state: dict, ref: object) -> Path:
    if isinstance(ref, dict):
        return SessionPaths.from_run_dir(state["data_dir"]).resolve_ref(ref["path_ref"])
    return Path(str(ref))


def _ref_json(state: dict, ref: object) -> dict:
    return json.loads(_ref_path(state, ref).read_text(encoding="utf-8"))


def _attempt_command(state: dict, attempt: dict) -> dict:
    return _ref_json(state, attempt["command_ref"])


def _attempt_execution(state: dict, attempt: dict) -> dict:
    return _ref_json(state, attempt["execution_ref"])


class AssemblyDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_assembly_candidates(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        expected = {
            "assembly.candidate.flye": "flye",
            "assembly.candidate.hifiasm": "hifiasm",
            "assembly.candidate.hifiasm_ont": "hifiasm",
            "assembly.candidate.nextdenovo": "nextdenovo",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].stage, "assembly")
            self.assertEqual(registry.methods[method_name].package_ref, package_name)
            self.assertIn(method_name, registry.by_stage["assembly"])
        hifiasm = registry.methods["assembly.candidate.hifiasm"]
        self.assertIn("assembly candidate", hifiasm.goal)
        self.assertIn("compatibility_route_explicitly_requested", hifiasm.review_rules)
        self.assertIn("explicit_hifi_or_non_ont_route_required", hifiasm.human_review_triggers)

    def test_shared_read_type_mapping_covers_manifest_platforms_and_methods(self):
        self.assertEqual(PLATFORM_TO_SHARED_READ_TYPE["cyclone"], "cyclone")
        self.assertEqual(PLATFORM_TO_SHARED_READ_TYPE["pacbio_hifi"], "pacbio_hifi")
        self.assertEqual(SHARED_TO_METHOD_READ_TYPE["cyclone"]["hifiasm_ont"], "ont")
        self.assertEqual(SHARED_TO_METHOD_READ_TYPE["ont_raw"]["hifiasm_ont"], "ont")
        self.assertEqual(SHARED_TO_METHOD_READ_TYPE["ont_hq"]["hifiasm_ont"], "ont")
        self.assertEqual(SHARED_TO_METHOD_READ_TYPE["pacbio_hifi"]["hifiasm"], "hifi")
        for shared_read_type in PLATFORM_TO_SHARED_READ_TYPE.values():
            with self.subTest(shared_read_type=shared_read_type):
                mapping = SHARED_TO_METHOD_READ_TYPE[shared_read_type]
                self.assertIn("flye", mapping)
                self.assertIn("nextdenovo", mapping)
                self.assertTrue({"hifiasm", "hifiasm_ont"} & set(mapping))


class AssemblyStageTests(unittest.TestCase):
    def _parameter_approval_request(self, blocked: dict) -> ResumeReviewRequest:
        """Bind an approval to the exact proposed parameter attempt."""

        self.assertEqual(blocked.get("supervision_state"), "blocked_human")
        interrupt = blocked.get("active_interrupt") or {}
        self.assertEqual(interrupt.get("gate_id"), "parameter_approval")
        metadata_keys = (
            "attempt_id",
            "task",
            "previous_attempt_id",
            "recommendation_hash",
        )
        for key in metadata_keys:
            self.assertIn(key, interrupt)
        self.assertIn("review_request_id", interrupt)
        self.assertIn("request_hash", interrupt)
        return ResumeReviewRequest(
            session_id=blocked["session_id"],
            stage="assembly",
            gate_id="parameter_approval",
            review_request_id=interrupt["review_request_id"],
            review_request_hash=interrupt["request_hash"],
            decision="approve_parameters",
            metadata={key: interrupt[key] for key in metadata_keys},
        )

    def _approve_parameter_gate(self, blocked: dict) -> dict:
        approval_update = resume_review_state(
            blocked,
            AssemblyStageTests._parameter_approval_request(self, blocked),
        )
        return {**blocked, **approval_update}

    def _invoke_with_parameter_approvals(self, state: dict) -> dict:
        """Run assembly, approving only the shared parameter gate when reached."""

        graph = build_stage()
        current = state
        for _ in range(8):
            result = graph.invoke(current)
            interrupt = result.get("active_interrupt") or {}
            if not (
                result.get("supervision_state") == "blocked_human"
                and interrupt.get("gate_id") == "parameter_approval"
            ):
                return result
            current = AssemblyStageTests._approve_parameter_gate(self, result)
        self.fail("assembly exceeded the bounded parameter-approval loop")

    def test_outer_graph_routes_to_method_subgraphs(self):
        outer_nodes = set(build_stage().get_graph().nodes)
        flye_nodes = set(build_flye_graph().get_graph().nodes)
        nextdenovo_nodes = set(build_nextdenovo_graph().get_graph().nodes)

        self.assertIn("flye", outer_nodes)
        self.assertIn("nextdenovo", outer_nodes)
        self.assertIn("hifiasm", outer_nodes)
        self.assertNotIn("method_application", outer_nodes)
        self.assertNotIn("freeze_flye_attempt", outer_nodes)
        self.assertNotIn("run_flye_attempt", outer_nodes)
        self.assertNotIn("collect_flye_artifacts", outer_nodes)
        self.assertIn("freeze", flye_nodes)
        self.assertIn("run", flye_nodes)
        self.assertIn("collect", flye_nodes)
        self.assertIn("freeze", nextdenovo_nodes)
        self.assertIn("run", nextdenovo_nodes)
        self.assertIn("collect", nextdenovo_nodes)
        hifiasm_nodes = set(build_hifiasm_graph().get_graph().nodes)
        self.assertIn("freeze", hifiasm_nodes)
        self.assertIn("run", hifiasm_nodes)
        self.assertIn("collect", hifiasm_nodes)

    def test_method_subgraphs_do_not_import_parent_graph_modules(self):
        for module in [flye_graph, nextdenovo_graph, hifiasm_graph]:
            source = inspect.getsource(module)
            self.assertNotIn("app.stages.assembly.nodes", source)
            self.assertNotIn("app.stages.assembly.routing", source)

    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "s1", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _state(self, tmpdir: str, registry_ref: str | None):
        run_dir = v2_run_dir(tmpdir, "s1")
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
        )
        state["current_stage"] = "assembly"
        state["asset_registry_ref"] = registry_ref
        state["stage_outputs"] = {"assembly": {"assembly_route": "cyclone"}}
        return state

    def _fake_flye_bin(self, tmpdir: str, *, fail: bool = False, marker: Path | None = None) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir()
        flye = bin_dir / "flye"
        if fail:
            flye.write_text(
                "#!/bin/sh\n"
                f"{f'echo flye-called > {marker!s}' if marker else ':'}\n"
                "echo fake flye failed >&2\n"
                "exit 7\n",
                encoding="utf-8",
            )
        else:
            flye.write_text(
                "#!/bin/sh\n"
                f"{f'echo flye-called > {marker!s}' if marker else ':'}\n"
                "outdir=\"\"\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = \"--out-dir\" ]; then\n"
                "    shift\n"
                "    outdir=\"$1\"\n"
                "  fi\n"
                "  shift\n"
                "done\n"
                "mkdir -p \"$outdir\"\n"
                "printf '>contig1\\nACGTACGT\\n' > \"$outdir/assembly.fasta\"\n"
                "printf 'fake flye log\\n' > \"$outdir/flye.log\"\n"
                "printf 'fake flye stdout\\n'\n",
                encoding="utf-8",
            )
        flye.chmod(0o755)
        return bin_dir

    def _fake_nextdenovo_bin(self, tmpdir: str, *, fail: bool = False, marker: Path | None = None) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(exist_ok=True)
        nextdenovo = bin_dir / "nextDenovo"
        if fail:
            nextdenovo.write_text(
                "#!/bin/sh\n"
                f"{f'echo nextdenovo-called > {marker!s}' if marker else ':'}\n"
                "echo fake nextDenovo failed >&2\n"
                "exit 9\n",
                encoding="utf-8",
            )
        else:
            nextdenovo.write_text(
                "#!/bin/sh\n"
                f"{f'echo nextdenovo-called > {marker!s}' if marker else ':'}\n"
                "cfg=\"$1\"\n"
                "workdir=$(awk -F= '/^workdir[[:space:]]*=/{gsub(/^[ \\t]+|[ \\t]+$/, \"\", $2); print $2}' \"$cfg\")\n"
                "mkdir -p \"$workdir/03.ctg_graph\"\n"
                "printf '>nd_contig1\\nACGTACGT\\n' > \"$workdir/03.ctg_graph/nd.asm.fasta\"\n"
                "printf 'fake nextDenovo stdout\\n'\n",
                encoding="utf-8",
            )
        nextdenovo.chmod(0o755)
        return bin_dir

    def _fake_hifiasm_bin(self, tmpdir: str, *, fail: bool = False, marker: Path | None = None) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(exist_ok=True)
        hifiasm = bin_dir / "hifiasm"
        if fail:
            hifiasm.write_text(
                "#!/bin/sh\n"
                f"{f'echo hifiasm-called > {marker!s}' if marker else ':'}\n"
                "echo fake hifiasm failed >&2\n"
                "exit 7\n",
                encoding="utf-8",
            )
        else:
            hifiasm.write_text(
                "#!/bin/sh\n"
                f"{f'echo hifiasm-called > {marker!s}' if marker else ':'}\n"
                "prefix=\"\"\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = \"-o\" ]; then shift; prefix=\"$1\"; fi\n"
                "  shift\n"
                "done\n"
                "printf 'S\\tcontig1\\tACGTACGT\\n' > \"$prefix.bp.p_ctg.gfa\"\n"
                "printf 'fake hifiasm stdout\\n'\n",
                encoding="utf-8",
            )
        hifiasm.chmod(0o755)
        return bin_dir

    def test_missing_assembly_inputs_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rnaseq = Path(tmpdir) / "rna.fq"
            rnaseq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_rnaseq_reads:rna.fq",
                        "asset_type": "raw_rnaseq_reads",
                        "path": str(rnaseq),
                    }
                ],
            )

            result = build_stage().invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "assembly")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("clean_long_reads or long_reads.fofn", result["last_error"])

    def test_plans_primary_and_one_fallback_from_fofn_and_genome_size_prior(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fofn = Path(tmpdir) / "long_reads.fofn"
            fofn.write_text(f"{Path(tmpdir) / 'reads.fastq'}\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "completed",
                    "assembly_input_ref": str(fofn),
                },
                "genome_survey": {
                    "status": "completed",
                    "mode": "full",
                    "genome_size_prior": 2_000_000,
                    "genome_size_source": "survey",
                    "estimates": {"genome_size": 2_000_000},
                    "result_artifact_ref": str(Path(tmpdir) / "genome_survey_result.json"),
                },
                "assembly": {
                    "assembly_route": "cyclone",
                    "method_decision": {
                        "primary": "assembly.candidate.flye",
                        "fallback": "assembly.candidate.nextdenovo",
                    }
                },
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "planned")
            self.assertEqual(assembly["primary_method"], "assembly.candidate.flye")
            self.assertEqual(assembly["fallback_method"], "assembly.candidate.nextdenovo")
            self.assertEqual(len(assembly["selected_candidate_refs"]), 2)
            self.assertLessEqual(len(assembly["selected_candidate_refs"]), 2)
            for candidate_ref in assembly["selected_candidate_refs"]:
                candidate = json.loads(Path(candidate_ref).read_text(encoding="utf-8"))
                self.assertEqual(candidate["status"], "planned")
                self.assertEqual(candidate["input_ref"], str(fofn))
                self.assertEqual(candidate["genome_size_prior"], 2_000_000)
                self.assertEqual(candidate["artifact_type"], "draft_assembly.candidate")
                self.assertTrue(candidate["expected_outputs"]["draft_assembly_fasta"].endswith(".fasta"))

    def test_missing_genome_size_rejects_methods_that_require_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fofn = Path(tmpdir) / "long_reads.fofn"
            fofn.write_text(f"{Path(tmpdir) / 'reads.fastq'}\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "completed",
                    "assembly_input_ref": str(fofn),
                },
                "assembly": {
                    "assembly_route": "cyclone",
                    "method_decision": {
                        "primary": "assembly.candidate.nextdenovo",
                    }
                },
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly")
            self.assertEqual(result["supervision_state"], "blocked_human")
            interrupt = result["active_interrupt"]
            self.assertEqual(interrupt["stage"], "assembly")
            self.assertIn("genome_size_required", interrupt["reason"])
            rejected = result["stage_outputs"]["assembly"]["rejected_candidates"]
            self.assertEqual(rejected["assembly.candidate.flye"], "genome_size_required")
            self.assertEqual(rejected["assembly.candidate.nextdenovo"], "genome_size_required")

    def test_auto_planning_without_human_decision_selects_no_more_than_primary_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly": {"assembly_route": "cyclone"},
                "genome_survey": {
                    "status": "completed",
                    "genome_size_prior": 2_000_000,
                    "genome_size_source": "survey",
                    "estimates": {"genome_size": 2_000_000},
                }
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(len(assembly["selected_candidate_refs"]), 2)
            self.assertEqual(assembly["primary_method"], "assembly.candidate.flye")
            self.assertEqual(assembly["fallback_method"], "assembly.candidate.nextdenovo")
            self.assertNotIn("assembly.candidate.hifiasm", assembly["candidate_methods"])
            self.assertEqual(set(assembly["candidate_methods"]), {
                "assembly.candidate.flye",
                "assembly.candidate.nextdenovo",
            })

    def test_explicit_hifiasm_request_remains_allowed_as_compatibility_route(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "pacbio_hifi",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly": {
                    "assembly_route": "hifi",
                    "method_decision": {
                        "primary": "assembly.candidate.hifiasm",
                    }
                }
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["primary_method"], "assembly.candidate.hifiasm")
            self.assertIn("assembly.candidate.hifiasm", assembly["selected_methods"])
            self.assertIn("assembly.candidate.hifiasm", assembly["candidate_methods"])

    def test_raw_long_reads_with_pacbio_hifi_platform_count_as_hifi_route(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_reads = Path(tmpdir) / "raw.fastq"
            raw_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:raw.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(raw_reads),
                        "platform": "pacbio_hifi",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly": {
                    "assembly_route": "hifi",
                    "method_decision": {
                        "primary": "assembly.candidate.hifiasm",
                    },
                }
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["primary_method"], "assembly.candidate.hifiasm")
            self.assertEqual(assembly["input_ref"], str(raw_reads))
            self.assertEqual(assembly["primary_long_read_platform"], "pacbio_hifi")
            self.assertIn("assembly.candidate.hifiasm", assembly["candidate_methods"])

    def test_auto_planning_without_genome_size_rejects_flye_and_nextdenovo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly")
            self.assertEqual(result["supervision_state"], "blocked_human")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(
                assembly["rejected_candidates"],
                {
                    "assembly.candidate.flye": "genome_size_required",
                    "assembly.candidate.nextdenovo": "genome_size_required",
                },
            )

    def test_default_planning_does_not_execute_flye(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            called_marker = Path(tmpdir) / "flye-called"
            fake_bin = self._fake_flye_bin(tmpdir, marker=called_marker)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly": {
                    "assembly_route": "cyclone",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                }
            }
            state["shared_parameters"] = {"estimated_genome_size": 2_000_000}

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            self.assertFalse(called_marker.exists())
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "planned")
            self.assertNotIn("draft_assembly_ref", assembly)
            candidate = json.loads(Path(assembly["selected_candidate_refs"][0]).read_text(encoding="utf-8"))
            self.assertEqual(candidate["status"], "planned")
            self.assertFalse(candidate["metadata"]["execution_enabled"])
            self.assertNotIn("execution_mode", candidate["metadata"])
            self.assertEqual(candidate["metadata"]["draft_assembly_ref_status"], "planned_path_not_materialized")

    def test_local_flye_rejects_invalid_threads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            fake_bin = self._fake_flye_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly": {
                    "assembly_route": "cyclone",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "parameter_recommendation": {"allow_model_unavailable_defaults": True},
                    "flye_parameters": {
                        "read_type": "nano-raw",
                        "estimated_genome_size": 2_000_000,
                        "threads": 0,
                    },
                }
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "blocked_param")
            invalid_params = {item["param"] for item in assembly.get("invalid_params") or []}
            self.assertIn("flye_parameters.threads", invalid_params)
            self.assertIn("flye_parameters.threads", result["last_error"])

    def test_local_flye_execution_writes_draft_assembly_and_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            fake_bin = self._fake_flye_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly": {
                    "assembly_route": "cyclone",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "parameter_recommendation": {"allow_model_unavailable_defaults": True},
                    "flye_parameters": {
                        "read_type": "nano-raw",
                        "estimated_genome_size": 2_000_000,
                        "threads": 1,
                    },
                }
            }

            blocked = build_stage().invoke(state)
            self.assertEqual(blocked["supervision_state"], "blocked_human")
            interrupt = blocked["active_interrupt"]
            self.assertEqual(interrupt["gate_id"], "parameter_approval")
            self.assertEqual(interrupt["task"], "recommend_initial")
            self.assertEqual(interrupt["attempt_id"], 0)
            self.assertIsNone(interrupt["previous_attempt_id"])
            approved = self._approve_parameter_gate(blocked)
            result = build_stage().invoke(approved)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "draft_ready")
            self.assertEqual(assembly["primary_method"], "assembly.candidate.flye")
            self.assertTrue(Path(assembly["draft_assembly_ref"]).exists())
            self.assertIn("executed_candidate_refs", assembly)
            self.assertNotIn("flye_frozen_attempt", assembly)
            self.assertNotIn("flye_execution_result", assembly)
            self.assertNotIn("env", assembly)
            self.assertEqual(assembly["parameter_attempts"][0]["method_id"], "assembly.candidate.flye")
            self.assertEqual(assembly["parameter_attempts"][0]["status"], "executed")
            self.assertIsNotNone(assembly["parameter_attempts"][0]["command_ref"])

            candidate = json.loads(Path(assembly["executed_candidate_refs"][0]).read_text(encoding="utf-8"))
            self.assertEqual(candidate["status"], "executed")
            self.assertTrue(Path(candidate["expected_outputs"]["draft_assembly_fasta"]).exists())
            self.assertTrue(candidate["metadata"]["execution_enabled"])
            self.assertNotIn("execution_mode", candidate["metadata"])
            self.assertEqual(candidate["metadata"]["draft_assembly_ref_status"], "materialized")
            self.assertEqual(len(assembly["candidate_artifact_refs"]), len(set(assembly["candidate_artifact_refs"])))

            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("assembly.draft_assembly", artifact_types)
            self.assertIn("draft_assembly.candidate", artifact_types)
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)
            self.assertIn("assembly.flye_log", artifact_types)

            ledger_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertIn("attempt_started", event_types)
            self.assertIn("attempt_succeeded", event_types)
            self.assertIn("artifact_registered", event_types)

    def test_local_flye_execution_from_read_qc_fofn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            fofn = Path(tmpdir) / "long_reads.fofn"
            fofn.write_text(f"{reads}\n", encoding="utf-8")
            fake_bin = self._fake_flye_bin(tmpdir)
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {"status": "completed", "assembly_input_ref": str(fofn)},
                "assembly": {
                    "assembly_route": "cyclone",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "parameter_recommendation": {"allow_model_unavailable_defaults": True},
                    "flye_parameters": {
                        "read_type": "nano-raw",
                        "estimated_genome_size": 2_000_000,
                        "threads": 1,
                    },
                },
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["input_type"], "long_reads.fofn")
            self.assertTrue(Path(assembly["draft_assembly_ref"]).exists())
            candidate = json.loads(Path(assembly["executed_candidate_refs"][0]).read_text(encoding="utf-8"))
            self.assertEqual(candidate["status"], "executed")

    def test_local_flye_execution_from_multi_line_read_qc_fofn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reads_1 = Path(tmpdir) / "reads_1.fastq"
            reads_2 = Path(tmpdir) / "reads_2.fastq"
            reads_1.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            reads_2.write_text("@r2\nTGCA\n+\n!!!!\n", encoding="utf-8")
            fofn = Path(tmpdir) / "long_reads.fofn"
            fofn.write_text(f"{reads_1}\n{reads_2}\n", encoding="utf-8")
            fake_bin = self._fake_flye_bin(tmpdir)
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {"status": "completed", "assembly_input_ref": str(fofn)},
                "assembly": {
                    "assembly_route": "cyclone",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "parameter_recommendation": {"allow_model_unavailable_defaults": True},
                    "flye_parameters": {
                        "read_type": "nano-raw",
                        "estimated_genome_size": 2_000_000,
                        "threads": 1,
                    },
                },
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            self.assertTrue(Path(assembly["draft_assembly_ref"]).exists())
            command = _attempt_command(result, assembly["parameter_attempts"][0])
            flye_script = Path(command["script_path"])
            script_text = flye_script.read_text(encoding="utf-8")
            self.assertIn(str(reads_1), script_text)
            self.assertIn(str(reads_2), script_text)

    def test_failed_local_flye_records_failed_attempt_without_fallback_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            fake_bin = self._fake_flye_bin(tmpdir, fail=True)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "status": "completed",
                    "genome_size_prior": 2_000_000,
                    "genome_size_source": "survey",
                    "estimates": {"genome_size": 2_000_000},
                },
                "assembly": {
                    "assembly_route": "cyclone",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "method_decision": {
                        "primary": "assembly.candidate.flye",
                        "fallback": "assembly.candidate.nextdenovo",
                    },
                    "parameter_recommendation": {
                        "allow_model_unavailable_defaults": True,
                        "max_execution_retries": 0,
                    },
                    "flye_parameters": {
                        "read_type": "nano-raw",
                        "estimated_genome_size": 2_000_000,
                        "threads": 1,
                    },
                },
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertEqual(result["last_error"], "execution_retry_budget_exhausted")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["parameter_attempts"][0]["method_id"], "assembly.candidate.flye")
            self.assertEqual(assembly["parameter_attempts"][0]["status"], "execution_failed")
            self.assertIsNotNone(assembly["parameter_attempts"][0]["command_ref"])
            execution = _attempt_execution(result, assembly["parameter_attempts"][0])["result"]
            self.assertIn("fake flye failed", Path(execution["stderr_path"]).read_text(encoding="utf-8"))
            artifact_index = Path(result["data_dir"]) / "artifacts" / "index.jsonl"
            artifact_types = {json.loads(line)["artifact_type"] for line in artifact_index.read_text().splitlines()}
            self.assertNotIn("assembly.draft_assembly", artifact_types)
            ledger_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertIn("attempt_started", event_types)
            self.assertIn("attempt_failed", event_types)
            self.assertNotIn("attempt_succeeded", event_types)

    def test_local_hifiasm_execution_writes_draft_assembly_and_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            flye_marker = Path(tmpdir) / "flye-called"
            self._fake_flye_bin(tmpdir, marker=flye_marker)
            fake_bin = self._fake_hifiasm_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "pacbio_hifi",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly": {
                    "assembly_route": "hifi",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "parameter_recommendation": {"allow_model_unavailable_defaults": True},
                }
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            self.assertFalse(flye_marker.exists())
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "draft_ready")
            self.assertEqual(assembly["primary_method"], "assembly.candidate.hifiasm")
            self.assertEqual(assembly["substeps"]["hifiasm_assembly"]["status"], "executed")
            draft = Path(assembly["draft_assembly_ref"])
            self.assertTrue(draft.exists())
            self.assertTrue(draft.read_text(encoding="utf-8").startswith(">contig1"))
            self.assertNotIn("hifiasm_frozen_attempt", assembly)
            self.assertNotIn("hifiasm_execution_result", assembly)

            candidate = json.loads(Path(assembly["executed_candidate_refs"][0]).read_text(encoding="utf-8"))
            self.assertEqual(candidate["status"], "executed")
            self.assertTrue(candidate["metadata"]["execution_enabled"])
            self.assertEqual(assembly["current_parameter_attempt_id"], 0)
            self.assertEqual(assembly["parameter_attempts"][0]["method_id"], "assembly.candidate.hifiasm")
            self.assertEqual(assembly["parameter_attempts"][0]["status"], "executed")
            self.assertIsNotNone(assembly["parameter_attempts"][0]["command_ref"])

            ledger_path = Path(result["data_dir"]) / "ledger" / "events.jsonl"
            events = [
                json.loads(line)["event_type"]
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("attempt_started", events)
            self.assertIn("attempt_succeeded", events)

    def test_local_hifiasm_execution_from_fofn_uses_ont_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reads = Path(tmpdir) / "ont.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            fofn = Path(tmpdir) / "long_reads.fofn"
            fofn.write_text(f"{reads}\n", encoding="utf-8")
            fake_bin = self._fake_hifiasm_bin(tmpdir)
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {"status": "completed", "assembly_input_ref": str(fofn)},
                "assembly": {
                    "assembly_route": "ont",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "parameter_recommendation": {"allow_model_unavailable_defaults": True},
                    "hifiasm_ont_parameters": {"read_type": "ont"},
                },
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["primary_method"], "assembly.candidate.hifiasm_ont")
            self.assertEqual(assembly["parameter_attempts"][0]["method_id"], "assembly.candidate.hifiasm_ont")
            self.assertEqual(assembly["parameter_attempts"][0]["status"], "executed")
            self.assertIsNotNone(assembly["parameter_attempts"][0]["command_ref"])
            command = _attempt_command(result, assembly["parameter_attempts"][0])
            script = Path(command["script_path"]).read_text(encoding="utf-8")
            self.assertIn("--ont", script)

    def test_local_hifiasm_rejects_invalid_threads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            fake_bin = self._fake_hifiasm_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "pacbio_hifi",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly": {
                    "assembly_route": "hifi",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "method_decision": {"primary": "assembly.candidate.hifiasm"},
                    "parameter_recommendation": {"allow_model_unavailable_defaults": True},
                    "hifiasm_parameters": {"read_type": "hifi", "threads": 0},
                }
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "blocked_param")

    def test_failed_local_hifiasm_records_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            fake_bin = self._fake_hifiasm_bin(tmpdir, fail=True)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "pacbio_hifi",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly": {
                    "assembly_route": "hifi",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "parameter_recommendation": {
                        "allow_model_unavailable_defaults": True,
                        "max_execution_retries": 0,
                    },
                }
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "execution_parameter_retries_exhausted")
            self.assertEqual(assembly["parameter_attempts"][0]["method_id"], "assembly.candidate.hifiasm")
            self.assertEqual(assembly["parameter_attempts"][0]["status"], "execution_failed")
            self.assertIsNotNone(assembly["parameter_attempts"][0]["command_ref"])
            execution = _attempt_execution(result, assembly["parameter_attempts"][0])["result"]
            self.assertIn("fake hifiasm failed", Path(execution["stderr_path"]).read_text(encoding="utf-8"))
            ledger_path = Path(result["data_dir"]) / "ledger" / "events.jsonl"
            events = [
                json.loads(line)["event_type"]
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("attempt_started", events)
            self.assertIn("attempt_failed", events)
            self.assertNotIn("attempt_succeeded", events)

    def test_local_nextdenovo_execution_writes_draft_assembly_and_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            called_marker = Path(tmpdir) / "nextdenovo-called"
            fake_bin = self._fake_nextdenovo_bin(tmpdir, marker=called_marker)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "status": "completed",
                    "genome_size_prior": 2_000_000,
                    "genome_size_source": "survey",
                    "estimates": {"genome_size": 2_000_000},
                },
                "assembly": {
                    "assembly_route": "cyclone",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "method_decision": {"primary": "assembly.candidate.nextdenovo"},
                    "parameter_recommendation": {"allow_model_unavailable_defaults": True},
                    "nextdenovo_parameters": {
                        "read_type": "ont",
                        "genome_size": 2_000_000,
                        "threads": 1,
                    },
                },
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            self.assertTrue(called_marker.exists())
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "draft_ready")
            self.assertEqual(assembly["primary_method"], "assembly.candidate.nextdenovo")
            self.assertEqual(assembly["substeps"]["nextdenovo_assembly"]["status"], "executed")
            self.assertEqual(assembly["parameter_attempts"][0]["method_id"], "assembly.candidate.nextdenovo")
            self.assertEqual(assembly["parameter_attempts"][0]["status"], "executed")
            self.assertIsNotNone(assembly["parameter_attempts"][0]["command_ref"])
            self.assertTrue(Path(assembly["draft_assembly_ref"]).exists())

            candidate = json.loads(Path(assembly["executed_candidate_refs"][0]).read_text(encoding="utf-8"))
            self.assertEqual(candidate["status"], "executed")
            self.assertTrue(candidate["metadata"]["execution_enabled"])
            self.assertTrue(candidate["metadata"]["method_graph_requested"])
            self.assertEqual(candidate["metadata"]["draft_assembly_ref_status"], "materialized")
            run_cfg = Path(candidate["logs"]["run_cfg"])
            self.assertTrue(run_cfg.exists())
            run_cfg_text = run_cfg.read_text(encoding="utf-8")
            self.assertIn("read_type = ont", run_cfg_text)
            self.assertIn("genome_size = 2000000", run_cfg_text)
            self.assertIn("read_cutoff = 1000", run_cfg_text)
            self.assertEqual(candidate["logs"]["run_cfg"], str(run_cfg))

            ledger_path = Path(result["data_dir"]) / "ledger" / "events.jsonl"
            events = [
                json.loads(line)["event_type"]
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("attempt_started", events)
            self.assertIn("attempt_succeeded", events)

    def test_local_nextdenovo_omitted_read_cutoff_uses_catalog_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            fake_bin = self._fake_nextdenovo_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "status": "completed",
                    "genome_size_prior": 2_000_000,
                    "genome_size_source": "survey",
                    "estimates": {"genome_size": 2_000_000},
                },
                "assembly": {
                    "assembly_route": "cyclone",
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "method_decision": {"primary": "assembly.candidate.nextdenovo"},
                    "parameter_recommendation": {"allow_model_unavailable_defaults": True},
                    "nextdenovo_parameters": {
                        "read_type": "ont",
                        "genome_size": 2_000_000,
                    },
                },
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            attempt = assembly["parameter_attempts"][0]
            command_payload = _attempt_command(result, attempt)
            candidate = json.loads(Path(assembly["executed_candidate_refs"][0]).read_text(encoding="utf-8"))
            run_cfg = Path(candidate["logs"]["run_cfg"])
            run_cfg_text = run_cfg.read_text(encoding="utf-8")
            self.assertIn("read_cutoff = 1000", run_cfg_text)
            self.assertIn("parallel_jobs = 32", run_cfg_text)
            self.assertIn("sort_options = -m 1g -t 32", run_cfg_text)
            self.assertIn("nextgraph_options = -a 1", run_cfg_text)
            self.assertEqual(command_payload["parameter_sources"]["l0_fixed"]["threads"], 32)
            self.assertNotIn("read_cutoff", command_payload["parameter_sources"]["l0_fixed"])
            self.assertEqual(sorted(command_payload["parameter_sources"]), ["l0_fixed", "l1_readonly", "l2_recommended"])
            self.assertEqual(command_payload["rendered_parameters"]["read_cutoff"], 1000)
            self.assertEqual(command_payload["rendered_parameters"]["nextgraph_a"], 1)
            self.assertEqual(command_payload["rendered_parameters"]["nextgraph_a"], 1)


class Phase4FlyeParameterTests(unittest.TestCase):
    """Phase 4 task03 coverage: Flye must not run without explicit params."""

    def _invoke_with_parameter_approvals(self, state: dict) -> dict:
        return AssemblyStageTests._invoke_with_parameter_approvals(self, state)

    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "s1", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _state(self, tmpdir: str, registry_ref: str):
        run_dir = v2_run_dir(tmpdir, "s1")
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
        )
        state["current_stage"] = "assembly"
        state["asset_registry_ref"] = registry_ref
        state["stage_outputs"] = {"assembly": {"assembly_route": "cyclone"}}
        return state

    def _fake_flye_bin(self, tmpdir: str, *, marker: Path | None = None) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir()
        flye = bin_dir / "flye"
        flye.write_text(
            "#!/bin/sh\n"
            f"{f'echo flye-called > {marker!s}' if marker else ':'}\n"
            "outdir=\"\"\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = \"--out-dir\" ]; then\n"
            "    shift\n"
            "    outdir=\"$1\"\n"
            "  fi\n"
            "  shift\n"
            "done\n"
            "mkdir -p \"$outdir\"\n"
            "printf '>contig1\\nACGTACGT\\n' > \"$outdir/assembly.fasta\"\n"
            "printf 'fake flye log\\n' > \"$outdir/flye.log\"\n"
            "printf 'fake flye stdout\\n'\n",
            encoding="utf-8",
        )
        flye.chmod(0o755)
        return bin_dir

    def _local_state_with_flye(self, tmpdir: str, *, flye_parameters: dict | None, marker: Path | None = None) -> dict:
        clean_reads = Path(tmpdir) / "clean.fastq"
        clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        fake_bin = self._fake_flye_bin(tmpdir, marker=marker)
        registry = self._write_registry(
            tmpdir,
            assets=[
                {
                    "asset_id": "s1:clean_long_reads:clean.fastq",
                    "asset_type": "clean_long_reads",
                    "path": str(clean_reads),
                    "platform": "cycloneseq",
                }
            ],
        )
        state = self._state(tmpdir, str(registry))
        assembly_out: dict = {
            "assembly_route": "cyclone",
            "execution_mode": "local",
            "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            "parameter_recommendation": {"allow_model_unavailable_defaults": True},
        }
        if flye_parameters is not None:
            assembly_out["flye_parameters"] = flye_parameters
        state["stage_outputs"] = {"assembly": assembly_out}
        return state

    def test_missing_flye_genome_size_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = Path(tmpdir) / "flye-called"
            state = self._local_state_with_flye(tmpdir, flye_parameters=None, marker=marker)

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertFalse(marker.exists())
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "blocked_human")
            self.assertEqual(assembly["rejected_candidates"]["assembly.candidate.flye"], "genome_size_required")
            self.assertEqual(assembly["read_type_prior"]["source"], "manifest_platform")
            self.assertEqual(assembly["read_type_prior"]["flye"], "nano-raw")

    def test_flye_omitted_threads_uses_catalog_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._local_state_with_flye(
                tmpdir,
                flye_parameters={
                    "read_type": "nano-hq",
                    "estimated_genome_size": 2_000_000,
                },
            )

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            attempt = assembly["parameter_attempts"][0]
            command = _attempt_command(result, attempt)
            flye_script = Path(command["script_path"])
            script_text = flye_script.read_text(encoding="utf-8")
            self.assertIn("--threads 32", script_text)
            self.assertEqual(command["parameter_sources"]["l0_fixed"]["threads"], 32)
            self.assertEqual(command["argv"][command["argv"].index("--threads") + 1], "32")

    def test_shared_parameters_fill_flye_read_type_and_genome_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._local_state_with_flye(
                tmpdir,
                flye_parameters={"threads": 1},
            )
            state["shared_parameters"] = {"read_type": "pacbio_hifi", "estimated_genome_size": 2_000_000}

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            command = _attempt_command(result, assembly["parameter_attempts"][0])
            flye_script = Path(command["script_path"])
            script_text = flye_script.read_text(encoding="utf-8")
            self.assertIn("--pacbio-hifi", script_text)
            self.assertIn("--genome-size 2000000", script_text)
            candidate = json.loads(Path(assembly["executed_candidate_refs"][0]).read_text(encoding="utf-8"))
            self.assertEqual(candidate["metadata"]["read_profile"], "pacbio-hifi")
            self.assertEqual(candidate["metadata"]["estimated_genome_size"], 2_000_000)

    def test_unknown_read_type_is_reported_as_invalid_param(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = Path(tmpdir) / "flye-called"
            state = self._local_state_with_flye(
                tmpdir,
                flye_parameters={
                    "read_type": "foo",
                    "estimated_genome_size": 2_000_000,
                    "threads": 1,
                },
                marker=marker,
            )

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertFalse(marker.exists())
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "blocked_param")
            invalid_by_param = {item["param"]: item for item in assembly["invalid_params"]}
            self.assertIn("flye_parameters.read_type", invalid_by_param)
            self.assertEqual(invalid_by_param["flye_parameters.read_type"]["value"], "foo")
            self.assertIn("nano-raw", invalid_by_param["flye_parameters.read_type"]["accepted"])

    def test_validated_params_are_passed_to_flye_command_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._local_state_with_flye(
                tmpdir,
                flye_parameters={
                    "read_type": "nano-hq",
                    "estimated_genome_size": 2_000_000,
                    "threads": 1,
                },
            )

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "draft_ready")
            command = _attempt_command(result, assembly["parameter_attempts"][0])
            flye_script = Path(command["script_path"])
            script_text = flye_script.read_text(encoding="utf-8")
            self.assertIn("--nano-hq", script_text)
            self.assertIn("--genome-size 2000000", script_text)
            self.assertIn("--threads 1", script_text)
            # pre-commit stage state still holds the rich metadata until commit
            # strips it; the executed Flye candidate bundle carries execution
            # provenance.
            candidate = json.loads(Path(assembly["executed_candidate_refs"][0]).read_text(encoding="utf-8"))
            self.assertTrue(candidate["metadata"]["execution_enabled"])
            self.assertNotIn("execution_mode", candidate["metadata"])
            # The executed bundle metadata must reflect the validated
            # parameters, not an unrelated default read profile.
            self.assertEqual(candidate["metadata"]["read_profile"], "nano-hq")
            self.assertEqual(candidate["metadata"]["estimated_genome_size"], 2_000_000)
            self.assertEqual(candidate["metadata"]["threads"], 1)

    def test_flye_command_keeps_reads_after_read_type_and_excludes_l1_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._local_state_with_flye(
                tmpdir,
                flye_parameters={
                    "read_type": "nano-hq",
                    "estimated_genome_size": 2_000_000,
                    "threads": 1,
                },
            )
            state["stage_outputs"]["read_qc"] = {
                "summary": {"n50": 10000},
            }

            result = self._invoke_with_parameter_approvals(state)

            self.assertEqual(result["current_stage"], "assembly_dedup")
            assembly = result["stage_outputs"]["assembly"]
            attempt = assembly["parameter_attempts"][0]
            command_payload = _attempt_command(result, attempt)
            argv = command_payload["argv"]
            read_flag_index = argv.index("--nano-hq")
            self.assertEqual(argv[read_flag_index + 1], assembly["input_ref"])
            self.assertIn("--out-dir", argv[read_flag_index + 2 :])
            self.assertNotIn("10000", argv)
            self.assertEqual(command_payload["parameter_sources"]["l1_readonly"]["read_n50"], 10000)
            self.assertNotIn("estimated_coverage", command_payload["parameter_sources"]["l1_readonly"])
            self.assertEqual(command_payload["argv"], argv)
            self.assertNotIn("10000", command_payload["command"])
            self.assertNotIn("35.5", command_payload["command"])


if __name__ == "__main__":
    unittest.main()
