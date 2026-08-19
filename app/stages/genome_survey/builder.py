"""Genome survey stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.execution.method_outcomes import method_outcome_settlement_node
from app.schemas.state import TopLevelState
from app.stages.genome_survey.method_application import (
    build_genomescope2_graph,
    build_jellyfish_graph,
)
from app.stages.genome_survey.method_application.genomescope2 import (
    METHOD_NAME as GENOMESCOPE2_METHOD_NAME,
)
from app.stages.genome_survey.method_application.genomescope2 import (
    genomescope2_outcome,
)
from app.stages.genome_survey.method_application.jellyfish import (
    METHOD_NAME as JELLYFISH_METHOD_NAME,
)
from app.stages.genome_survey.method_application.jellyfish import (
    jellyfish_outcome,
)
from app.stages.genome_survey.nodes import (
    commit_genome_survey,
    enter_genome_survey,
    fail_genome_survey,
    plan_full_kmer_survey,
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


def build_stage() -> CompiledStateGraph[
    TopLevelState, None, TopLevelState, TopLevelState
]:
    """Build full survey with a no-readable-input fallback to an explicit prior."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_genome_survey)
    graph.add_node("resolve_survey_inputs", resolve_survey_inputs)
    graph.add_node("select_survey_mode", select_survey_mode)
    graph.add_node("plan_full_kmer_survey", plan_full_kmer_survey)
    graph.add_node("jellyfish", build_jellyfish_graph())
    graph.add_node("genomescope2", build_genomescope2_graph())
    graph.add_node(
        "settle_jellyfish_outcome",
        method_outcome_settlement_node(
            stage="genome_survey",
            method_name=JELLYFISH_METHOD_NAME,
            outcome_policy=jellyfish_outcome,
        ),
    )
    graph.add_node(
        "settle_genomescope2_outcome",
        method_outcome_settlement_node(
            stage="genome_survey",
            method_name=GENOMESCOPE2_METHOD_NAME,
            outcome_policy=genomescope2_outcome,
        ),
    )
    graph.add_node("write_full_survey_result", write_full_survey_result)
    graph.add_node("write_survey_skip", write_survey_skip)
    graph.add_node("commit", commit_genome_survey)
    graph.add_node("fail", fail_genome_survey)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_survey_inputs")
    graph.add_conditional_edges(
        "resolve_survey_inputs",
        route_after_resolve,
        {"select": "select_survey_mode", "fail": "settle_jellyfish_outcome"},
    )
    graph.add_conditional_edges(
        "select_survey_mode",
        route_after_select,
        {
            "full": "plan_full_kmer_survey",
            "skip": "write_survey_skip",
            "fail": "settle_jellyfish_outcome",
        },
    )
    graph.add_conditional_edges(
        "plan_full_kmer_survey",
        route_after_full_plan,
        {
            "jellyfish": "jellyfish",
            "commit": "settle_jellyfish_outcome",
            "fail": "settle_jellyfish_outcome",
        },
    )
    graph.add_conditional_edges(
        "jellyfish",
        route_after_jellyfish,
        {"genomescope2": "genomescope2", "fail": "settle_jellyfish_outcome"},
    )
    graph.add_conditional_edges(
        "genomescope2",
        route_after_genomescope2,
        {
            "write_result": "write_full_survey_result",
            "fail": "settle_jellyfish_outcome",
        },
    )
    graph.add_conditional_edges(
        "write_full_survey_result",
        route_after_plan,
        {"commit": "settle_jellyfish_outcome", "fail": "settle_jellyfish_outcome"},
    )
    graph.add_conditional_edges(
        "write_survey_skip",
        route_after_plan,
        {"commit": "settle_jellyfish_outcome", "fail": "settle_jellyfish_outcome"},
    )
    graph.add_edge("settle_jellyfish_outcome", "settle_genomescope2_outcome")
    graph.add_conditional_edges(
        "settle_genomescope2_outcome",
        route_after_plan,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
