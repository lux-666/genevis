"""Registry for reusable human review gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable

from app.schemas.state import TopLevelState

DecisionApplier = Callable[[TopLevelState, Any], dict[str, Any]]


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


def registered_review_stage_label() -> str:
    stages = registered_review_stages()
    return ", ".join(stages) if stages else "<none>"


def review_prompt_bundle(stage: str, gate_id: str | None = None) -> str | None:
    return get_review_gate(stage, gate_id).prompt_bundle


def apply_review_gate_decision(state: TopLevelState, request: Any) -> dict[str, Any]:
    gate_id = getattr(request, "gate_id", None)
    active_interrupt = state.get("active_interrupt") or {}
    if isinstance(active_interrupt, dict):
        gate_id = active_interrupt.get("gate_id") or gate_id
    gate = get_review_gate(request.stage, gate_id)
    return gate.load_decision_applier()(state, request)


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
    )
)

register_review_gate(
    ReviewGateDeclaration(
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        decisions_allowed=("select_method", "skip", "stop_human_review"),
        decision_applier="app.stages.hic_scaffolding.nodes:apply_hic_scaffolding_resume_decision",
        prompt_adapter="hic_scaffolding",
        template_preferred_decision="select_method",
        required_resume_fields=("method_name",),
    )
)
