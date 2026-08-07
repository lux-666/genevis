"""Registry for reusable human review gates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import import_module
from typing import TYPE_CHECKING, Any, Callable, cast

from pydantic import BaseModel

from app.schemas.state import (
    DOMAIN_STAGES,
    DomainStage,
    StateUpdate,
    TopLevelState,
)

if TYPE_CHECKING:
    from app.review.application import (
        PreparedReviewApplication,
        ReviewIntentV1,
        VerifiedReviewApplication,
    )
    from app.review.base import ResumeReviewRequest

InterruptContextBuilder = Callable[[Mapping[str, Any]], dict[str, Any]]
PrepareApplication = Callable[
    [TopLevelState, "ReviewIntentV1"], "PreparedReviewApplication"
]
ApplyApplication = Callable[[TopLevelState, "VerifiedReviewApplication"], StateUpdate]
TimeoutRequestNormalizer = Callable[
    [TopLevelState, "ResumeReviewRequest", dict[str, Any]], "ResumeReviewRequest"
]
IntentRequestValidator = Callable[
    [TopLevelState, "ResumeReviewRequest", Mapping[str, Any]],
    "ResumeReviewRequest",
]


@dataclass(frozen=True)
class ReviewGateDeclaration:
    """Static behavior declaration for a review gate.

    Runtime request data remains in ``ReviewPayload`` and ledger/artifacts.
    The registry only records how a gate is resumed, recommended, and prompted.
    """

    stage: DomainStage
    gate_id: str
    decisions_allowed: tuple[str, ...]
    intent_metadata_model: str
    validate_intent_request: str
    effect_kind: str
    effect_model: str
    prepare_application: str
    apply_application: str
    prompt_bundle: str | None = None
    prompt_adapter: str = "generic"
    template_preferred_decision: str | None = None
    required_resume_fields_by_decision: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    normalize_timeout_request: str | None = None
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

    def load_prepare_application(self) -> PrepareApplication:
        return cast(PrepareApplication, self._load_callable(self.prepare_application))

    def load_validate_intent_request(self) -> IntentRequestValidator:
        return cast(
            IntentRequestValidator,
            self._load_callable(self.validate_intent_request),
        )

    def load_apply_application(self) -> ApplyApplication:
        return cast(ApplyApplication, self._load_callable(self.apply_application))

    def load_timeout_request_normalizer(self) -> TimeoutRequestNormalizer | None:
        if not self.normalize_timeout_request:
            return None
        return cast(
            TimeoutRequestNormalizer,
            self._load_callable(self.normalize_timeout_request),
        )

    def decode_effect(self, value: Any) -> BaseModel:
        module_name, class_name = self.effect_model.rsplit(":", 1)
        model = getattr(import_module(module_name), class_name)
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise TypeError(
                f"review gate effect_model is not a Pydantic model: {self.effect_model}"
            )
        return model.model_validate(value)

    def decode_intent_metadata(self, value: Any) -> BaseModel:
        module_name, class_name = self.intent_metadata_model.rsplit(":", 1)
        model = getattr(import_module(module_name), class_name)
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise TypeError(
                "review gate intent_metadata_model is not a Pydantic model: "
                f"{self.intent_metadata_model}"
            )
        return model.model_validate(value)

    def required_resume_fields_for(self, decision: str) -> tuple[str, ...]:
        return tuple(self.required_resume_fields_by_decision.get(decision, ()))

    def declared_resume_fields(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                field_name
                for fields in self.required_resume_fields_by_decision.values()
                for field_name in fields
            )
        )

    @staticmethod
    def _load_callable(path: str) -> Callable[..., Any]:
        module_name, function_name = path.rsplit(":", 1)
        function = getattr(import_module(module_name), function_name)
        if not callable(function):
            raise TypeError(f"review gate hook is not callable: {path}")
        return cast(Callable[..., Any], function)


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


register_review_gate(
    ReviewGateDeclaration(
        stage="assembly",
        gate_id="parameter_approval",
        decisions_allowed=("approve_parameters", "reject_parameters"),
        intent_metadata_model="app.review.approval:ParameterApprovalMetadataV1",
        validate_intent_request=(
            "app.stages.assembly.parameters.workflow:"
            "validate_assembly_parameter_intent_request"
        ),
        effect_kind="assembly.parameter_approval.v1",
        effect_model="app.review.approval:ParameterApprovalEffectV1",
        prepare_application=(
            "app.stages.assembly.parameters.workflow:"
            "prepare_parameter_approval_application"
        ),
        apply_application=(
            "app.stages.assembly.parameters.workflow:"
            "apply_parameter_approval_application"
        ),
        normalize_timeout_request=(
            "app.review.approval:normalize_parameter_approval_timeout_request"
        ),
        prompt_adapter="generic",
        template_preferred_decision="approve_parameters",
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
        intent_metadata_model="app.review.application:EmptyReviewMetadataV1",
        validate_intent_request=(
            "app.stages.inventory.nodes:validate_inventory_review_intent_request"
        ),
        effect_kind="inventory.review_application.v1",
        effect_model="app.stages.inventory.nodes:InventoryReviewApplicationEffectV1",
        prepare_application=(
            "app.stages.inventory.nodes:prepare_inventory_review_application"
        ),
        apply_application="app.stages.inventory.nodes:apply_inventory_review_application",
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
        intent_metadata_model=(
            "app.stages.assembly_review.nodes:AssemblyReviewIntentMetadataV1"
        ),
        validate_intent_request=(
            "app.stages.assembly_review.nodes:validate_assembly_review_intent_request"
        ),
        effect_kind="assembly_review.review_application.v1",
        effect_model="app.stages.assembly_review.nodes:AssemblyReviewEffectV1",
        prepare_application=(
            "app.stages.assembly_review.nodes:prepare_assembly_review_application"
        ),
        apply_application=(
            "app.stages.assembly_review.nodes:apply_assembly_review_application"
        ),
        interrupt_context_builder=(
            "app.review.interrupt_context:assembly_review_interrupt_context"
        ),
        prompt_bundle="review.assembly_review_recommend",
        prompt_adapter="assembly_review",
        template_preferred_decision="accept",
        required_resume_fields_by_decision={
            "accept": ("candidate_id",),
            "pass": ("candidate_id",),
            "purge": ("candidate_id",),
            "polish": ("candidate_id",),
            "continue_low_confidence": ("candidate_id",),
        },
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
        intent_metadata_model=(
            "app.stages.assembly.nodes:AssemblyRouteSelectionMetadataV1"
        ),
        validate_intent_request=(
            "app.stages.assembly.nodes:validate_route_selection_intent_request"
        ),
        effect_kind="assembly.route_selection.v1",
        effect_model=(
            "app.stages.assembly.nodes:AssemblyRouteSelectionApplicationEffectV1"
        ),
        prepare_application=(
            "app.stages.assembly.nodes:prepare_route_selection_application"
        ),
        apply_application="app.stages.assembly.nodes:apply_route_selection_application",
        interrupt_context_builder=(
            "app.review.interrupt_context:assembly_route_selection_interrupt_context"
        ),
        normalize_timeout_request=(
            "app.stages.assembly.nodes:normalize_selection_timeout_request"
        ),
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
        intent_metadata_model="app.review.approval:ParameterApprovalMetadataV1",
        validate_intent_request=(
            "app.stages.hic_scaffolding.parameters.workflow:"
            "validate_autohic_parameter_intent_request"
        ),
        effect_kind="hic_scaffolding.parameter_approval.v1",
        effect_model="app.review.approval:ParameterApprovalEffectV1",
        prepare_application=(
            "app.stages.hic_scaffolding.parameters.workflow:"
            "prepare_autohic_parameter_approval_application"
        ),
        apply_application=(
            "app.stages.hic_scaffolding.parameters.workflow:"
            "apply_autohic_parameter_approval_application"
        ),
        normalize_timeout_request=(
            "app.review.approval:normalize_parameter_approval_timeout_request"
        ),
        interrupt_context_builder=(
            "app.review.interrupt_context:hic_scaffolding_interrupt_context"
        ),
        prompt_adapter="generic",
        template_preferred_decision="approve_parameters",
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
        intent_metadata_model=("app.review.application:ForegroundReviewMetadataV1"),
        validate_intent_request=(
            "app.stages.hic_scaffolding.nodes:"
            "validate_hic_scaffolding_review_intent_request"
        ),
        effect_kind="hic_scaffolding.review_application.v1",
        effect_model=("app.stages.hic_scaffolding.nodes:HiCScaffoldingReviewEffectV1"),
        prepare_application=(
            "app.stages.hic_scaffolding.nodes:"
            "prepare_hic_scaffolding_review_application"
        ),
        apply_application=(
            "app.stages.hic_scaffolding.nodes:apply_hic_scaffolding_review_application"
        ),
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
        intent_metadata_model="app.review.approval:ParameterApprovalMetadataV1",
        validate_intent_request=(
            "app.stages.t2t.parameters.workflow:validate_t2t_parameter_intent_request"
        ),
        effect_kind="t2t.parameter_approval.v1",
        effect_model="app.review.approval:ParameterApprovalEffectV1",
        prepare_application=(
            "app.stages.t2t.parameters.workflow:"
            "prepare_t2t_parameter_approval_application"
        ),
        apply_application=(
            "app.stages.t2t.parameters.workflow:"
            "apply_t2t_parameter_approval_application"
        ),
        normalize_timeout_request=(
            "app.review.approval:normalize_parameter_approval_timeout_request"
        ),
        prompt_adapter="generic",
        template_preferred_decision="approve_parameters",
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
        intent_metadata_model=("app.review.application:ForegroundReviewMetadataV1"),
        validate_intent_request=("app.stages.t2t.nodes:validate_t2t_intent_request"),
        effect_kind="t2t.review_application.v1",
        effect_model="app.stages.t2t.nodes:T2TReviewEffectV1",
        prepare_application="app.stages.t2t.nodes:prepare_t2t_review_application",
        apply_application="app.stages.t2t.nodes:apply_t2t_review_application",
        prompt_adapter="t2t",
        template_preferred_decision="accept_selected_candidates",
        required_resume_fields_by_decision={
            "accept_selected_candidates": ("candidate_ids",),
        },
        proposal_kind="finishing_candidate_review",
        proposal_label="T2T finishing candidate review",
        parameter_recommendation_status="implemented",
        related_parameter_gate=("t2t", "parameter_approval"),
        plan_order=30,
    )
)
