"""Registry for reusable human review gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable

from app.schemas.state import TopLevelState

DecisionApplier = Callable[[TopLevelState, Any], dict[str, Any]]
TimeoutApplier = Callable[[TopLevelState, Any, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ReviewGateDeclaration:
    """Static behavior declaration for a review gate.

    Runtime request data remains in ``ReviewPayload`` and ledger/artifacts.
    The registry only records how a gate is resumed, recommended, and prompted.
    """

    stage: str
    gate_id: str
    decisions_allowed: tuple[str, ...]
    decision_applier: str
    prompt_bundle: str | None = None
    prompt_adapter: str = "generic"
    template_preferred_decision: str | None = None
    required_resume_fields: tuple[str, ...] = field(default_factory=tuple)
    timeout_applier: str | None = None
    proposal_kind: str = "human_review"
    proposal_label: str | None = None
    parameter_recommendation_status: str = "not_applicable"
    related_parameter_gate: tuple[str, str] | None = None
    parameter_recommendation_notes: tuple[str, ...] = field(default_factory=tuple)
    plan_order: int = 100

    @property
    def key(self) -> tuple[str, str]:
        return self.stage, self.gate_id

    def load_decision_applier(self) -> DecisionApplier:
        module_name, function_name = self.decision_applier.rsplit(":", 1)
        module = import_module(module_name)
        applier = getattr(module, function_name)
        if not callable(applier):
            raise TypeError(f"review gate applier is not callable: {self.decision_applier}")
        return applier

    def load_timeout_applier(self) -> TimeoutApplier | None:
        if not self.timeout_applier:
            return None
        module_name, function_name = self.timeout_applier.rsplit(":", 1)
        module = import_module(module_name)
        applier = getattr(module, function_name)
        if not callable(applier):
            raise TypeError(f"review gate timeout applier is not callable: {self.timeout_applier}")
        return applier


_DECLARATIONS: dict[tuple[str, str], ReviewGateDeclaration] = {}


def register_review_gate(declaration: ReviewGateDeclaration) -> ReviewGateDeclaration:
    if not declaration.stage:
        raise ValueError("review gate stage is required")
    if not declaration.gate_id:
        raise ValueError("review gate gate_id is required")
    if declaration.key in _DECLARATIONS:
        raise ValueError(f"review gate already registered: {declaration.stage}/{declaration.gate_id}")
    _DECLARATIONS[declaration.key] = declaration
    return declaration


def get_review_gate(stage: str, gate_id: str | None = None) -> ReviewGateDeclaration:
    gate = find_review_gate(stage, gate_id)
    if gate is None:
        suffix = f"/{gate_id}" if gate_id else ""
        raise KeyError(f"review gate is not registered: {stage}{suffix}")
    return gate


def find_review_gate(stage: str | None, gate_id: str | None = None) -> ReviewGateDeclaration | None:
    if not stage:
        return None
    if gate_id:
        return _DECLARATIONS.get((stage, gate_id))
    matches = [gate for (registered_stage, _), gate in _DECLARATIONS.items() if registered_stage == stage]
    if len(matches) == 1:
        return matches[0]
    return None


def is_registered_review_gate(stage: str | None, gate_id: str | None = None) -> bool:
    return find_review_gate(stage, gate_id) is not None


def registered_review_stages() -> tuple[str, ...]:
    return tuple(dict.fromkeys(stage for stage, _ in _DECLARATIONS))


def planned_review_gates(stages: list[str] | tuple[str, ...]) -> list[ReviewGateDeclaration]:
    """Return registered runtime gates in workflow and declaration order."""

    stage_order = {stage: index for index, stage in enumerate(stages)}
    indexed = [
        (index, gate)
        for index, gate in enumerate(_DECLARATIONS.values())
        if gate.stage in stage_order
    ]
    return [
        gate
        for _, gate in sorted(
            indexed,
            key=lambda item: (stage_order[item[1].stage], item[1].plan_order, item[0]),
        )
    ]


def registered_review_stage_label() -> str:
    stages = registered_review_stages()
    return ", ".join(stages) if stages else "<none>"


def review_prompt_bundle(stage: str, gate_id: str | None = None) -> str | None:
    return get_review_gate(stage, gate_id).prompt_bundle


def apply_review_gate_decision(state: TopLevelState, request: Any) -> dict[str, Any]:
    gate_id = getattr(request, "gate_id", None)
    active_interrupt = state.get("active_interrupt") or {}
    if isinstance(active_interrupt, dict) and active_interrupt.get("gate_id") == "parameter_approval":
        if gate_id != "parameter_approval":
            raise ValueError("parameter approval resume requires explicit gate_id=parameter_approval")
    elif isinstance(active_interrupt, dict):
        gate_id = active_interrupt.get("gate_id") or gate_id
    gate = get_review_gate(request.stage, gate_id)
    return gate.load_decision_applier()(state, request)


def apply_review_gate_timeout(state: TopLevelState, request: Any, timeout: dict[str, Any]) -> dict[str, Any] | None:
    gate_id = getattr(request, "gate_id", None)
    active_interrupt = state.get("active_interrupt") or {}
    if isinstance(active_interrupt, dict):
        gate_id = active_interrupt.get("gate_id") or gate_id
    gate = get_review_gate(request.stage, gate_id)
    applier = gate.load_timeout_applier()
    if applier is None:
        return None
    return applier(state, request, timeout)


register_review_gate(
    ReviewGateDeclaration(
        stage="assembly",
        gate_id="parameter_approval",
        decisions_allowed=("approve_parameters", "reject_parameters"),
        decision_applier="app.stages.assembly.nodes:apply_parameter_approval_decision",
        prompt_adapter="generic",
        proposal_kind="parameter_recommendation",
        proposal_label="Assembly parameter proposal",
        parameter_recommendation_status="implemented",
        plan_order=20,
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="inventory",
        gate_id="inventory_review",
        decisions_allowed=("continue", "stop_human_review"),
        decision_applier="app.stages.inventory.nodes:apply_inventory_resume_decision",
        template_preferred_decision="continue",
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="assembly_review",
        gate_id="assembly_review",
        decisions_allowed=(
            "accept",
            "pass",
            "purge",
            "polish",
            "continue_low_confidence",
            "stop_human_review",
            "rerun",
        ),
        decision_applier="app.stages.assembly_review.nodes:apply_assembly_review_resume_decision",
        prompt_bundle="review.assembly_review_recommend",
        prompt_adapter="assembly_review",
        template_preferred_decision="accept",
        required_resume_fields=("candidate_id", "final_assembly_ref"),
        proposal_kind="assembly_candidate_review",
        proposal_label="Assembly result and evidence review",
        parameter_recommendation_status="mixed",
        related_parameter_gate=("assembly", "parameter_approval"),
        parameter_recommendation_notes=(
            "Phase 11 quality-gate failures route to assembly parameter recommendation and approval.",
            "A human-selected rerun still uses the legacy manual Flye parameter path.",
        ),
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="assembly",
        gate_id="route_selection",
        decisions_allowed=("select_cyclone", "select_ont", "select_hifi", "stop_human_review"),
        decision_applier="app.stages.assembly.nodes:apply_route_decision",
        timeout_applier="app.stages.assembly.nodes:apply_selection_timeout",
        prompt_bundle="assembly.route_selector",
        prompt_adapter="generic",
        template_preferred_decision="select_ont",
        plan_order=10,
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="hic_scaffolding",
        gate_id="parameter_approval",
        decisions_allowed=("approve_parameters", "reject_parameters"),
        decision_applier=(
            "app.stages.hic_scaffolding.parameter_nodes:"
            "apply_autohic_parameter_approval_decision"
        ),
        prompt_adapter="generic",
        proposal_kind="parameter_recommendation",
        proposal_label="AutoHiC restriction-enzyme proposal",
        parameter_recommendation_status="implemented",
        plan_order=20,
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        decisions_allowed=(
            "accept_candidate",
            "keep_prior_assembly",
            "stop_human_review",
        ),
        decision_applier="app.stages.hic_scaffolding.nodes:apply_hic_scaffolding_resume_decision",
        prompt_adapter="hic_scaffolding",
        template_preferred_decision="accept_candidate",
        proposal_kind="autohic_result_review",
        proposal_label="AutoHiC chromosome-assembly adoption review",
        parameter_recommendation_status="implemented",
        related_parameter_gate=("hic_scaffolding", "parameter_approval"),
        plan_order=30,
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="t2t",
        gate_id="t2t_review",
        decisions_allowed=("accept_selected_candidates", "continue_without_patch", "stop_pipeline"),
        decision_applier="app.stages.t2t.nodes:apply_t2t_review_decision",
        prompt_adapter="t2t",
        template_preferred_decision="accept_selected_candidates",
        required_resume_fields=("candidate_ids",),
        proposal_kind="finishing_candidate_review",
        proposal_label="T2T finishing candidate review",
        parameter_recommendation_status="not_implemented",
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="repeat_annotation",
        gate_id="mask_review",
        decisions_allowed=("accept", "skip"),
        decision_applier="app.stages.repeat_annotation.nodes:apply_mask_review_decision",
        template_preferred_decision="accept",
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="gene_annotation",
        gate_id="gene_method_planning_review",
        decisions_allowed=("select_methods", "accept", "stop"),
        decision_applier="app.stages.gene_annotation.nodes:apply_gene_method_planning_review_decision",
        template_preferred_decision="select_methods",
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="gene_annotation",
        gate_id="candidate_evidence_review",
        decisions_allowed=("proceed_merge", "request_rerun", "stop"),
        decision_applier="app.stages.gene_annotation.nodes:apply_candidate_evidence_review_decision",
        template_preferred_decision="proceed_merge",
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="gene_annotation",
        gate_id="merge_review",
        decisions_allowed=("accept_weights", "adjust_weights", "stop"),
        decision_applier="app.stages.gene_annotation.nodes:apply_merge_review_decision",
        template_preferred_decision="accept_weights",
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="gene_annotation",
        gate_id="final_gene_set_review",
        decisions_allowed=("accept", "reject", "stop"),
        decision_applier="app.stages.gene_annotation.nodes:apply_final_gene_set_review_decision",
        template_preferred_decision="accept",
    )
)
