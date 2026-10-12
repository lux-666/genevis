"""Execute native Trio/Hi-C command contracts and verify separate deliveries."""

import gzip
import os
import sys

import pytest

from tests import test_assembly as fixture


def _fastq(path, sequence="ACGT", compressed=False):
    payload = f"@r1\n{sequence}\n+\n{'I' * len(sequence)}\n"
    if compressed:
        path.write_bytes(gzip.compress(payload.encode()))
    else:
        path.write_text(payload)
    return str(path)


def _state(tmp_path, mode, *, missing_hap2=False):
    helper = fixture.AssemblyStageTests()
    hifi = _fastq(tmp_path / "child hifi.fq")
    ont = _fastq(tmp_path / "ul ONT.fq")
    assets = [
        {
            "asset_id": "child",
            "asset_type": "raw_long_reads",
            "path": hifi,
            "platform": "pacbio_hifi",
            "format": "fastq",
        },
        {
            "asset_id": "ul",
            "asset_type": "raw_long_reads",
            "path": ont,
            "platform": "ont",
            "format": "fastq",
        },
    ]
    inputs = {"ultralong_ont_reads_ref": [ont]}
    if mode == "trio":
        for role, count in (("paternal", 3), ("maternal", 2)):
            paths = []
            for index in range(count):
                path = _fastq(tmp_path / f"{role} {index}.fq", compressed=index == 1)
                paths.append(path)
                assets.append(
                    {
                        "asset_id": f"{role}-{index}",
                        "asset_type": "raw_short_dna_reads",
                        "path": path,
                        "platform": "illumina",
                        "format": "fastq",
                        "metadata": {"intake_role": f"trio_{role}_reads"},
                    }
                )
            inputs[f"trio_{role}_reads_ref"] = paths
    else:
        hic_paths = []
        for index, mate in enumerate(("R1", "R2")):
            path = _fastq(tmp_path / f"hic {mate}.fq")
            hic_paths.append(path)
            assets.append(
                {
                    "asset_id": mate,
                    "asset_type": "hic_reads_registered",
                    "path": path,
                    "platform": "illumina",
                    "format": "fastq",
                    "metadata": {
                        "manifest_entry_ordinal": 2,
                        "pair_index": 0,
                        "mate": mate,
                        "path_ordinal": index,
                    },
                }
            )
        inputs["hic_reads_ref"] = hic_paths
    registry = helper._write_registry(str(tmp_path), assets=assets)
    state = helper._state(str(tmp_path), registry)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "hifiasm"
    tool.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "assert '--ul' in args and '--ont' not in args\n"
        "prefix = args[args.index('-o') + 1]\n"
        "if '-1' in args:\n"
        "    assert '-2' in args and '--h1' not in args\n"
        "    assert Path(args[args.index('-1') + 1]).read_text() == '3'\n"
        "    assert Path(args[args.index('-2') + 1]).read_text() == '2'\n"
        "else:\n"
        "    assert '--h1' in args and '--h2' in args\n"
        "    prefix += '.hic'\n"
        "Path(prefix + '.hap1.p_ctg.gfa').write_text('S\\thap1\\tACGT\\n')\n"
        + (
            ""
            if missing_hap2
            else "Path(prefix + '.hap2.p_ctg.gfa').write_text('S\\thap2\\tTGCA\\n')\n"
        )
    )
    tool.chmod(0o755)
    yak = bin_dir / "yak"
    yak.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "assert args[:4] == ['count', '-k31', '-b37', '-t2']\n"
        "assert len(args) == 7, 'all parent files must be streamed into one input'\n"
        "reads = Path(args[-1]).read_text()\n"
        "Path(args[args.index('-o') + 1]).write_text(str(reads.count('>')))\n"
    )
    yak.chmod(0o755)
    state["stage_seeds"] = {"assembly": {"hifiasm_inputs": inputs}}
    state["stage_outputs"] = {
        "assembly": {
            "assembly_route": "hifi",
            "threads": 2,
            "execution_mode": "local",
            "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
            "assembly_selection": fixture._assembly_selection(
                read_type="pacbio_hifi", primary_method="assembly.candidate.hifiasm"
            ),
        }
    }
    if mode == "hic":
        state["resource_refs"]["hic_reads"] = {
            "resource_type": "hic_reads",
            "paths": hic_paths,
            "source_stage": "inventory",
            "workflow_step_id": "step-000-inventory",
            "metadata": {"asset_ids": ["R1", "R2"]},
        }
    return helper, state


@pytest.mark.parametrize("mode", ["trio", "hic"])
def test_joint_inputs_publish_distinct_haplotypes(tmp_path, mode):
    helper, state = _state(tmp_path, mode)
    result = helper._invoke_with_parameter_approvals(state)
    assert result["current_stage"] == "assembly_dedup", result.get("last_error")
    output = result["stage_outputs"]["assembly"]
    refs = output["phased_output_refs"]
    hap1 = fixture._ref_path(result, refs[f"{mode}.hap1_fasta"][0])
    hap2 = fixture._ref_path(result, refs[f"{mode}.hap2_fasta"][0])
    assert hap1.read_text() == ">hap1\nACGT\n"
    assert hap2.read_text() == ">hap2\nTGCA\n"
    bundle = fixture._ref_json(result, output["executed_candidate_bundle_ref"])
    primary = fixture._ref_path(result, bundle["primary_output"])
    assert primary.read_bytes() == hap1.read_bytes()
    command = fixture._attempt_command(result, output["parameter_attempts"][0])
    assert command["phasing_input_kind"] == mode
    execution = fixture._attempt_execution(result, output["parameter_attempts"][0])
    assert execution is not None


@pytest.mark.parametrize("mode", ["trio", "hic"])
def test_missing_haplotype_fails_delivery(tmp_path, mode):
    helper, state = _state(tmp_path, mode, missing_hap2=True)
    result = helper._invoke_with_parameter_approvals(state)
    assert result["supervision_state"] == "failed_terminal"
    assert not result["stage_outputs"]["assembly"].get("executed_candidate_bundle_ref")


def test_public_joint_config_seeds_json_roles_and_rejects_conflicting_phasing():
    from app.control.stage_seeds import stage_seeds_from_run_config
    from app.schemas.run_config import RunConfig

    config = RunConfig.model_validate(
        {
            "requested_stages": [{"stage": "assembly", "allow_local_execution": True}],
            "assembly": {
                "hifiasm_inputs": {"ultralong_ont_reads_ref": ["/reads/ul.fq"]}
            },
        }
    )
    seeds = stage_seeds_from_run_config(config, shared_parameters=None)
    assert seeds["assembly"]["hifiasm_inputs"] == {
        "ultralong_ont_reads_ref": ["/reads/ul.fq"]
    }
    with pytest.raises(ValueError, match="one Assembly phasing mode"):
        config.assembly.hifiasm_inputs.model_validate(
            {
                "trio_maternal_reads_ref": ["/mother.fq"],
                "trio_paternal_reads_ref": ["/father.fq"],
                "hic_reads_ref": ["/hic1.fq"],
            }
        )


def test_trio_collection_replays_after_work_outputs_are_consumed(tmp_path):
    from app.stages.assembly.method_application.hifiasm import collect_hifiasm_artifacts

    helper, state = _state(tmp_path, "trio")
    result = helper._invoke_with_parameter_approvals(state)
    assert result["current_stage"] == "assembly_dedup", result.get("last_error")
    output = result["stage_outputs"]["assembly"]
    original_refs = output["phased_output_refs"]
    result["current_stage"] = "assembly"
    result["workflow_step_index"] = 1
    result["entered_workflow_step_index"] = 1
    result["stage_outputs"]["assembly"]["status"] = "hifiasm_ran"
    replay = collect_hifiasm_artifacts(result)["stage_outputs"]["assembly"]
    assert (
        replay["executed_candidate_bundle_ref"]
        == output["executed_candidate_bundle_ref"]
    )
    assert replay["phased_output_refs"] == original_refs


@pytest.mark.parametrize(
    "role", ["use_ultralong_ont", "use_hic_phasing", "trio_enabled"]
)
def test_human_selection_cannot_drop_joint_materials_in_baseline_mode(role):
    from app.control.errors import ResumeValidationError
    from app.review.base import ResumeReviewRequest
    from app.stages.assembly.nodes import _requested_method

    request = ResumeReviewRequest(
        session_id="s1",
        stage="assembly",
        gate_id="assembly_route_selection",
        decision="select_hifi",
        method_name="assembly.candidate.flye",
    )
    with pytest.raises(ResumeValidationError, match="not available"):
        _requested_method(request, {role: True, "has_hifi": True}, "pacbio_hifi")
