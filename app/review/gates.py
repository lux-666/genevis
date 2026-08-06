"""Registry for reusable human review gates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable, cast

from app.control.workflow_plan import current_workflow_step_id
from app.schemas.state import (
    DOMAIN_STAGES,
    DomainStage,
    StateUpdate,
    TopLevelState,
)

DecisionApplier = Callable[[TopLevelState, Any], StateUpdate]
TimeoutApplier = Callable[[TopLevelState, Any, dict[str, Any]], StateUpdate]
InterruptContextBuilder = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ReviewGateDeclaration:
    """Static behavior declaration for a review gate.

    Runtime request data remains in ``ReviewPayload`` and ledger/artifacts.
    The registry only records how a gate is resumed, recommended, and prompted.
    """

    stage: DomainStage
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
    interrupt_context_builder: str | None = None

    @property
    def key(self) -> tuple[DomainStage, str]:
        return self.stage, self.gate_id

    def load_decision_applier(self) -> DecisionApplier:
        module_name, function_name = self.decision_applier.rsplit(":", 1)
        module = import_module(module_name)
        applier = getattr(module, function_name)
        if not callable(applier):
            raise TypeError(
                f"review gate applier is not callable: {self.decision_applier}"
            )
        return cast(DecisionApplier, applier)

    def load_timeout_applier(self) -> TimeoutApplier | None:
        if not self.timeout_applier:
            return None
        module_name, function_name = self.timeout_applier.rsplit(":", 1)
        module = import_module(module_name)
        applier = getattr(module, function_name)
        if not callable(applier):
            raise TypeError(
                f"review gate timeout applier is not callable: {self.timeout_applier}"
            )
        return cast(TimeoutApplier, applier)

    def load_interrupt_context_builder(
        self,
    ) -> InterruptContextBuilder | None:
        """Load the stage-specific interrupt context builder, if registered."""
        if not self.interrupt_context_builder:
            return None
        module_name, function_name = self.interrupt_context_builder.rsplit(":", 1)
        module = import_module(module_name)
        builder = getattr(module, function_name)
        if not callable(builder):
            raise TypeError(
                "interrupt_context_builder is not callable: "
                f"{self.interrupt_context_builder}"
            )
        return cast(InterruptContextBuilder, builder)


_DECLARATIONS: dict[tuple[DomainStage, str], ReviewGateDeclaration] = {}


def register_review_gate(declaration: ReviewGateDeclaration) -> ReviewGateDeclaration:
    if not declaration.stage:
        raise ValueError("review gate stage is required")
    if declaration.stage not in DOMAIN_STAGES:
        raise ValueError(
            f"review gate stage is not a current domain stage: {declaration.stage}"
        )
    if not declaration.gate_id:
        raise ValueError("review gate gate_id is required")
    if declaration.key in _DECLARATIONS:
        raise ValueError(
            f"review gate already registered: {declaration.stage}/{declaration.gate_id}"
        )
    _DECLARATIONS[declaration.key] = declaration
    return declaration


def get_review_gate(stage: str, gate_id: str | None = None) -> ReviewGateDeclaration:
    gate = find_review_gate(stage, gate_id)
    if gate is None:
        suffix = f"/{gate_id}" if gate_id else ""
        raise KeyError(f"review gate is not registered: {stage}{suffix}")
    return gate


def find_review_gate(
    stage: str | None, gate_id: str | None = None
) -> ReviewGateDeclaration | None:
    if not stage:
        return None
    if gate_id:
        if stage not in DOMAIN_STAGES:
            return None
        return _DECLARATIONS.get((cast(DomainStage, stage), gate_id))
    matches = [
        gate
        for (registered_stage, _), gate in _DECLARATIONS.items()
        if registered_stage == stage
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def is_registered_review_gate(stage: str | None, gate_id: str | None = None) -> bool:
    return find_review_gate(stage, gate_id) is not None


def registered_review_stages() -> tuple[DomainStage, ...]:
    return tuple(dict.fromkeys(stage for stage, _ in _DECLARATIONS))


def planned_review_gates(
    stages: list[str] | tuple[str, ...],
) -> list[ReviewGateDeclaration]:
    """Return registered runtime gates in workflow and declaration order."""

    declarations = tuple(_DECLARATIONS.values())
    return _planned_review_gates_in_workflow_order(stages, declarations)


def _planned_review_gates_in_workflow_order(
    stages: list[str] | tuple[str, ...],
    declarations: tuple[ReviewGateDeclaration, ...],
) -> list[ReviewGateDeclaration]:
    """Preserve each planned stage occurrence; a stage may repeat in the plan."""

    planned: list[ReviewGateDeclaration] = []
    for stage in stages:
        planned.extend(
            sorted(
                (gate for gate in declarations if gate.stage == stage),
                key=lambda gate: gate.plan_order,
            )
        )
    return planned


def registered_review_stage_label() -> str:
    stages = registered_review_stages()
    return ", ".join(stages) if stages else "<none>"


def review_prompt_bundle(stage: str, gate_id: str | None = None) -> str | None:
    """Return the optional LLM recommendation bundle for a registered gate."""

    return get_review_gate(stage, gate_id).prompt_bundle


def apply_review_gate_decision(
    state: TopLevelState,
    request: Any,
) -> StateUpdate:
    _validate_review_occurrence(state, request)
    gate_id = getattr(request, "gate_id", None)
    active_interrupt = state.get("active_interrupt")
    if active_interrupt and active_interrupt.get("gate_id") == "parameter_approval":
        if gate_id != "parameter_approval":
            raise ValueError(
                "parameter approval resume requires explicit gate_id=parameter_approval"
            )
    elif active_interrupt:
        gate_id = active_interrupt.get("gate_id") or gate_id
    gate = get_review_gate(request.stage, gate_id)
    return gate.load_decision_applier()(state, request)


def apply_review_gate_timeout(
    state: TopLevelState, request: Any, timeout: dict[str, Any]
) -> StateUpdate | None:
    _validate_review_occurrence(state, request)
    gate_id = getattr(request, "gate_id", None)
    active_interrupt = state.get("active_interrupt")
    if active_interrupt:
        gate_id = active_interrupt.get("gate_id") or gate_id
    gate = get_review_gate(request.stage, gate_id)
    applier = gate.load_timeout_applier()
    if applier is None:
        return None
    return applier(state, request, timeout)


def _validate_review_occurrence(state: TopLevelState, request: Any) -> None:
    """Reject a review resume targeting another workflow occurrence."""

    stage = getattr(request, "stage", None)
    if not isinstance(stage, str) or not stage:
        raise ValueError("review request has no stage")
    current_step_id = current_workflow_step_id(state, expected_stage=stage)
    active_interrupt = state.get("active_interrupt")
    if not active_interrupt:
        return
    if active_interrupt.get("stage") != stage:
        raise ValueError("active review interrupt stage does not match the request")
    interrupt_step_id = active_interrupt.get("workflow_step_id")
    if not isinstance(interrupt_step_id, str) or not interrupt_step_id:
        raise ValueError("active review interrupt has no workflow_step_id")
    if interrupt_step_id != current_step_id:
        raise ValueError("active review interrupt targets a different workflow step")


register_review_gate(
    ReviewGateDeclaration(
        stage="assembly",
        gate_id="parameter_approval",
        decisions_allowed=("approve_parameters", "reject_parameters"),
        decision_applier=(
            "app.stages.assembly.parameters.workflow:apply_parameter_approval_decision"
        ),
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
        ),
        decision_applier="app.stages.assembly_review.nodes:apply_assembly_review_resume_decision",
        interrupt_context_builder=(
            "app.review.interrupt_context:assembly_review_interrupt_context"
        ),
        prompt_bundle="review.assembly_review_recommend",
        prompt_adapter="assembly_review",
        template_preferred_decision="accept",
        required_resume_fields=("candidate_id",),
        proposal_kind="assembly_candidate_review",
        proposal_label="Assembly result and evidence review",
        parameter_recommendation_status="mixed",
        related_parameter_gate=("assembly", "parameter_approval"),
        parameter_recommendation_notes=(
            "Assembly quality-gate failures route through the canonical v3 "
            "parameter recommendation and approval workflow.",
        ),
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="assembly",
        gate_id="route_selection",
        decisions_allowed=(
            "select_cyclone",
            "select_ont",
            "select_hifi",
            "stop_human_review",
        ),
        decision_applier="app.stages.assembly.nodes:apply_route_decision",
        interrupt_context_builder=(
            "app.review.interrupt_context:assembly_route_selection_interrupt_context"
        ),
        timeout_applier="app.stages.assembly.nodes:apply_selection_timeout",
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
            "app.stages.hic_scaffolding.parameters.workflow:"
            "apply_autohic_parameter_approval_decision"
        ),
        interrupt_context_builder=(
            "app.review.interrupt_context:hic_scaffolding_interrupt_context"
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
            "request_parameter_retry",
            "stop_human_review",
        ),
        decision_applier="app.stages.hic_scaffolding.nodes:apply_hic_scaffolding_resume_decision",
        interrupt_context_builder=(
            "app.review.interrupt_context:hic_scaffolding_interrupt_context"
        ),
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
        gate_id="parameter_approval",
        decisions_allowed=("approve_parameters", "reject_parameters"),
        decision_applier=(
            "app.stages.t2t.parameters.workflow:apply_t2t_parameter_approval_decision"
        ),
        prompt_adapter="generic",
        proposal_kind="parameter_recommendation",
        proposal_label="T2T threshold proposal",
        parameter_recommendation_status="implemented",
        plan_order=20,
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="t2t",
        gate_id="t2t_review",
        decisions_allowed=(
            "accept_selected_candidates",
            "continue_without_patch",
            "stop_pipeline",
        ),
        decision_applier="app.stages.t2t.nodes:apply_t2t_review_decision",
        prompt_adapter="t2t",
        template_preferred_decision="accept_selected_candidates",
        required_resume_fields=("candidate_ids",),
        proposal_kind="finishing_candidate_review",
        proposal_label="T2T finishing candidate review",
        parameter_recommendation_status="implemented",
        related_parameter_gate=("t2t", "parameter_approval"),
        plan_order=30,
    )
)
