"""Genome survey stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.genome_survey.method_application import (
    build_genomescope2_graph,
    build_jellyfish_graph,
)
from app.stages.genome_survey.nodes import (
    commit_genome_survey,
    enter_genome_survey,
    fail_genome_survey,
    plan_full_kmer_survey,
    plan_long_read_fallback,
    resolve_survey_inputs,
    select_survey_mode,
    write_full_survey_result,
    write_survey_skip,
)
from app.stages.genome_survey.routing import (
    route_after_full_plan,
    route_after_genomescope2,
    route_after_jellyfish,
    route_after_plan,
    route_after_resolve,
    route_after_select,
)


def build_stage():
    """Build the genome survey subgraph for full/fallback/skip contracts."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_genome_survey)
    graph.add_node("resolve_survey_inputs", resolve_survey_inputs)
    graph.add_node("select_survey_mode", select_survey_mode)
    graph.add_node("plan_full_kmer_survey", plan_full_kmer_survey)
    graph.add_node("jellyfish", build_jellyfish_graph())
    graph.add_node("genomescope2", build_genomescope2_graph())
    graph.add_node("write_full_survey_result", write_full_survey_result)
    graph.add_node("plan_long_read_fallback", plan_long_read_fallback)
    graph.add_node("write_survey_skip", write_survey_skip)
    graph.add_node("commit", commit_genome_survey)
    graph.add_node("fail", fail_genome_survey)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_survey_inputs")
    graph.add_conditional_edges(
        "resolve_survey_inputs",
        route_after_resolve,
        {"select": "select_survey_mode", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "select_survey_mode",
        route_after_select,
        {
            "full": "plan_full_kmer_survey",
            "fallback": "plan_long_read_fallback",
            "skip": "write_survey_skip",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "plan_full_kmer_survey",
        route_after_full_plan,
        {"jellyfish": "jellyfish", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "jellyfish",
        route_after_jellyfish,
        {"genomescope2": "genomescope2", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "genomescope2",
        route_after_genomescope2,
        {"write_result": "write_full_survey_result", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "write_full_survey_result",
        route_after_plan,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_long_read_fallback",
        route_after_plan,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "write_survey_skip",
        route_after_plan,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
