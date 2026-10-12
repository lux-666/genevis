"""Clean paired reads must reach Jellyfish with artifact identity and gzip decoding."""

import os
import sys
from pathlib import Path

from app.stages.genome_survey.method_application.jellyfish import (
    freeze_jellyfish_attempt,
)
from app.stages.genome_survey.nodes import resolve_survey_inputs
from app.stages.read_qc.builder import build_stage
from tests import test_read_qc as fixture


def test_fastp_to_survey_preserves_current_paths_compression_and_dependencies(tmp_path):
    helper = fixture.ReadQCSeqKitStageTests()
    state = helper._short_pair_state(str(tmp_path))
    # Emit real gzip bytes; a filename-only fake cannot prove this handoff.
    fastp = tmp_path / "fastp-bin" / "fastp"
    fastp.write_text(
        f"#!{sys.executable}\n"
        "import gzip, json, sys\n"
        "args = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
        "for mate in ('1', '2'):\n"
        "    with gzip.open(args['--out' + mate], 'wb') as out:\n"
        "        out.write(open(args['--in' + mate], 'rb').read())\n"
        "open(args['--json'], 'w').write(json.dumps({'summary': {"
        "'before_filtering': {'total_reads': 2, 'total_bases': 8},"
        "'after_filtering': {'total_reads': 2, 'total_bases': 8}}}))\n"
        "open(args['--html'], 'w').write('<html>QC</html>')\n"
    )
    state["workflow_steps"].append(
        {
            "step_id": "step-002-genome_survey",
            "stage": "genome_survey",
            "allow_local_execution": True,
            "selected_method_names": [
                "genome_survey.kmer_count.jellyfish",
                "genome_survey.model.genomescope2",
            ],
        }
    )
    cleaned = build_stage().invoke(state)
    clean_paths = cleaned["resource_refs"]["short_reads"]["paths"]
    cleaned["workflow_step_index"] = 2
    cleaned["entered_workflow_step_index"] = 2
    cleaned["current_stage"] = "genome_survey"
    resolved = resolve_survey_inputs(cleaned)
    output = resolved["stage_outputs"]["genome_survey"]
    assert output["status"] == "resolved"
    members = output["resolved_assets"]["short_reads"]
    assert [m["path"] for m in members] == clean_paths
    assert all(m["compression"] == "gzip" and m["read_artifact_ref"] for m in members)
    bin_dir = tmp_path / "survey-bin"
    bin_dir.mkdir()
    for name in ("jellyfish",):
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\necho jellyfish 2.3\n")
        tool.chmod(0o755)
    output.update(
        {
            "status": "local_execution_planned",
            "survey_input_family": "short_reads",
            "survey_parameters": {"kmer_size": 21, "hash_size": "1M", "threads": 1},
            "execution_mode": "local",
            "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        }
    )
    prepared = {**cleaned, "stage_outputs": resolved["stage_outputs"]}
    frozen = freeze_jellyfish_attempt(prepared)
    frozen_output = frozen["stage_outputs"]["genome_survey"]
    assert frozen_output["status"] == "jellyfish_frozen", frozen_output
    script = next(Path(cleaned["data_dir"]).rglob("run_jellyfish.sh")).read_text()
    assert script.count("gzip -cd") == 4
    assert all(path in script for path in clean_paths)
