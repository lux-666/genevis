"""LangGraph builder for the T2T telomere-finishing stage."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.t2t.nodes import (
    apply_t2t_patches,
    build_t2t_alignment_graph,
    commit_t2t,
    enter_t2t,
    fail_t2t,
    gate_t2t_review,
    prepare_t2t_reads,
    resolve_t2t_inputs,
    scan_assembly_telomeres,
    scan_read_telomeres,
    validate_patched_assembly,
    write_t2t_report,
)
from app.stages.t2t.routing import (
    route_after_alignment,
    route_after_assembly_scan,
    route_after_gate,
    route_after_patches,
    route_after_read_prepare,
    route_after_read_scan,
    route_after_report,
    route_after_resolve,
    route_after_validation,
)


def build_stage():
    """Build the scan -> evidence -> optional patch T2T subgraph."""
    graph = StateGraph(TopLevelState)
    graph.add_node("enter", enter_t2t)
    graph.add_node("resolve_inputs", resolve_t2t_inputs)
    graph.add_node("scan_assembly", scan_assembly_telomeres)
    graph.add_node("prepare_reads", prepare_t2t_reads)
    graph.add_node("scan_reads", scan_read_telomeres)
    graph.add_node("alignment", build_t2t_alignment_graph())
    graph.add_node("apply_patches", apply_t2t_patches)
    graph.add_node("validate_patched", validate_patched_assembly)
    graph.add_node("report", write_t2t_report)
    graph.add_node("gate_review", gate_t2t_review)
    graph.add_node("commit", commit_t2t)
    graph.add_node("fail", fail_t2t)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_inputs")
    graph.add_conditional_edges("resolve_inputs", route_after_resolve, {"scan_assembly": "scan_assembly", "report": "report", "fail": "fail"})
    graph.add_conditional_edges("scan_assembly", route_after_assembly_scan, {"prepare_reads": "prepare_reads", "fail": "fail"})
    graph.add_conditional_edges("prepare_reads", route_after_read_prepare, {"scan_reads": "scan_reads", "report": "report", "fail": "fail"})
    graph.add_conditional_edges("scan_reads", route_after_read_scan, {"alignment": "alignment", "fail": "fail"})
    graph.add_conditional_edges("alignment", route_after_alignment, {"apply_patches": "apply_patches", "report": "report", "fail": "fail"})
    graph.add_conditional_edges("apply_patches", route_after_patches, {"validate_patched": "validate_patched", "report": "report", "fail": "fail"})
    graph.add_conditional_edges("validate_patched", route_after_validation, {"report": "report", "fail": "fail"})
    graph.add_conditional_edges("report", route_after_report, {"gate_review": "gate_review", "commit": "commit", "fail": "fail"})
    graph.add_conditional_edges("gate_review", route_after_gate, {"block": END, "commit": "commit"})
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)
    return graph.compile()
