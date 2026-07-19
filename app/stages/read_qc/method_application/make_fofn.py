"""Local FOFN materialization method subgraph."""

from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, START, StateGraph

from app.storage.ledger import Ledger
from app.storage.artifacts import publish_artifact_records
from app.schemas.artifacts import READ_QC_FILTERED_LONG_READS_FOFN, READ_QC_LONG_READS_FOFN, ArtifactMeta
from app.schemas.state import TopLevelState
from app.stages.base import stage_artifacts_dir
from app.stages.read_qc.constants import MAKE_FOFN_METHOD_NAME
from app.stages.read_qc.method_application.lifecycle import read_paths_from_output, read_qc_output, update_read_qc_output


def build_make_fofn_graph():
    """Build the one-node local materialization subgraph."""
    graph = StateGraph(TopLevelState)
    graph.add_node("make", make_fofn)
    graph.add_edge(START, "make")
    graph.add_edge("make", END)
    return graph.compile()


def make_fofn(state: TopLevelState) -> dict:
    output = read_qc_output(state)
    if output.get("status") not in {"length_filter_skipped", "length_filter_executed"}:
        return {}
    session_id = state["session_id"]
    data_dir = Path(state.get("data_dir", "data"))
    artifact_dir = stage_artifacts_dir(state, "read_qc")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if output.get("filtered_long_reads_ref"):
        paths = read_paths_from_output(output, "filtered_long_reads_refs", "filtered_long_reads_ref")
        fofn_path = artifact_dir / "filtered_long_reads.fofn"
        artifact_type, ref_key = READ_QC_FILTERED_LONG_READS_FOFN, "filtered_long_reads_fofn_ref"
    else:
        paths = read_paths_from_output(output, "raw_long_reads_paths", "raw_long_reads_path")
        fofn_path = artifact_dir / "long_reads.fofn"
        artifact_type, ref_key = READ_QC_LONG_READS_FOFN, "long_reads_fofn_ref"
    fofn_path.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
    artifact = ArtifactMeta.from_file(fofn_path, stage="read_qc", method_name=MAKE_FOFN_METHOD_NAME, artifact_type=artifact_type,
                                      metadata={"source_read_ref": str(paths[0]), "source_read_refs": [str(path) for path in paths]})
    publish_artifact_records(data_dir, session_id, [artifact])
    ledger = Ledger(data_dir)
    ledger.create_session(session_id, state.get("input_manifest_ref", ""))
    ledger.record_artifact_registered(session_id, artifact.model_dump(mode="json"))
    substeps = dict(output.get("substeps") or {})
    substeps["make_fofn"] = {"name": "make_fofn", "status": "executed", "method_name": MAKE_FOFN_METHOD_NAME, "artifact_ref": str(fofn_path)}
    return update_read_qc_output(state, {"status": "fofn_ready", ref_key: str(fofn_path), "assembly_input_ref": str(fofn_path), "substeps": substeps})
