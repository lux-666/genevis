"""Parent LangGraph builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graph.checkpointer import (
    DEFAULT_CHECKPOINTER_BACKEND,
    CheckpointerBackend,
    get_checkpointer,
)
from app.graph.routing import route_controller, route_from_current_stage
from app.schemas.state import TopLevelState
from app.stages.assembly.builder import build_stage as build_assembly_stage
from app.stages.assembly_review.builder import build_stage as build_assembly_review_stage
from app.stages.function_annotation.builder import build_stage as build_function_annotation_stage
from app.stages.gene_annotation.builder import build_stage as build_gene_annotation_stage
from app.stages.genome_survey.builder import build_stage as build_genome_survey_stage
from app.stages.hic_scaffolding.builder import build_stage as build_hic_scaffolding_stage
from app.stages.inventory.builder import build_stage as build_inventory_stage
from app.stages.read_qc.builder import build_stage as build_read_qc_stage
from app.stages.repeat_annotation.builder import build_stage as build_repeat_annotation_stage


def build_graph(
    *,
    checkpointer=None,
    backend: CheckpointerBackend = DEFAULT_CHECKPOINTER_BACKEND,
    data_dir: str = "data",
):
    """Build the thin parent graph.

    ``checkpointer`` overrides backend resolution when provided (used by
    tests). ``backend`` and ``data_dir`` are otherwise passed to the
    factory so a CLI run can choose sqlite or memory persistence.
    """
    graph = StateGraph(TopLevelState)

    graph.add_node("route_controller", route_controller)
    graph.add_node("inventory_stage", build_inventory_stage())
    graph.add_node("read_qc_stage", build_read_qc_stage())
    graph.add_node("genome_survey_stage", build_genome_survey_stage())
    graph.add_node("assembly_stage", build_assembly_stage())
    graph.add_node("assembly_review_stage", build_assembly_review_stage())
    graph.add_node("hic_scaffolding_stage", build_hic_scaffolding_stage())
    graph.add_node("repeat_annotation_stage", build_repeat_annotation_stage())
    graph.add_node("gene_annotation_stage", build_gene_annotation_stage())
    graph.add_node("function_annotation_stage", build_function_annotation_stage())

    graph.add_edge(START, "route_controller")
    graph.add_conditional_edges(
        "route_controller",
        route_from_current_stage,
        {
            "inventory_stage": "inventory_stage",
            "read_qc_stage": "read_qc_stage",
            "genome_survey_stage": "genome_survey_stage",
            "assembly_stage": "assembly_stage",
            "assembly_review_stage": "assembly_review_stage",
            "hic_scaffolding_stage": "hic_scaffolding_stage",
            "repeat_annotation_stage": "repeat_annotation_stage",
            "gene_annotation_stage": "gene_annotation_stage",
            "function_annotation_stage": "function_annotation_stage",
            "end": END,
        },
    )

    for node_name in [
        "inventory_stage",
        "read_qc_stage",
        "genome_survey_stage",
        "assembly_stage",
        "assembly_review_stage",
        "hic_scaffolding_stage",
        "repeat_annotation_stage",
        "gene_annotation_stage",
        "function_annotation_stage",
    ]:
        graph.add_edge(node_name, "route_controller")

    return graph.compile(
        checkpointer=(
            checkpointer
            if checkpointer is not None
            else get_checkpointer(backend, data_dir=data_dir)
        )
    )
