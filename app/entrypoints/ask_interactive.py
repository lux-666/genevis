"""Interactive TTY loop for `gene-agent ask`."""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from app.entrypoints.ask_planner import (
    auto_resolve_plan,
    merge_clarification_response,
    next_clarification,
    save_plan_to_default_path,
)
from app.schemas.ask_plan import AskClarificationRequest, AskClarificationResponse, AskPlanState

try:
    import questionary
    _HAS_QUESTIONARY = True
except ImportError:
    _HAS_QUESTIONARY = False


def is_interactive(args: argparse.Namespace) -> bool:
    if getattr(args, "json", False):
        return False
    if getattr(args, "non_interactive", False):
        return False
    return sys.stdin.isatty()


def run_interactive_loop(plan: AskPlanState, args: argparse.Namespace) -> int:
    original_plan = plan
    auto_resolved = auto_resolve_plan(plan)
    plan = _apply_set_params(auto_resolved, getattr(args, "set_params", None) or [])

    # Store auto-resolved hints for re-use after rejection
    _auto_hint = auto_resolved if auto_resolved.selected_stages is not None else None

    if plan.selected_stages is not None and original_plan.selected_stages is None:
        if not _confirm_auto_resolve(plan):
            plan = original_plan
            plan = _apply_set_params(plan, getattr(args, "set_params", None) or [])

    plan_ref = save_plan_to_default_path(plan)

    while True:
        clarification = next_clarification(plan, ask_plan_ref=plan_ref)
        if clarification is None:
            break

        if clarification.kind == "confirm":
            _render_confirm(clarification)
            if not _prompt_confirm("Proceed?"):
                print("\nRestarting from stage selection.\n")
                plan = plan.model_copy(update={
                    "selected_stages": None,
                    "resolved_execution_order": None,
                    "selected_methods": None,
                    "method_parameters": None,
                    "confirmed": False,
                })
                plan_ref = save_plan_to_default_path(plan)
                continue
            response = AskClarificationResponse(kind="confirm", confirmed=True)
        else:
            response = _prompt_for_answer(clarification)

        plan = merge_clarification_response(plan, response)

        # After stage_selection, auto-fill method if it matches the hint
        if response.kind == "stage_selection" and _auto_hint is not None:
            plan = _try_auto_fill_methods(plan, _auto_hint)

        plan_ref = save_plan_to_default_path(plan)

    return _execute_confirmed_plan(plan, args)


def _try_auto_fill_methods(plan: AskPlanState, hint: AskPlanState) -> AskPlanState:
    """If user-selected stages match the auto-resolved hint, carry over methods."""
    if plan.selected_methods is not None:
        return plan
    if not hint.selected_methods:
        return plan
    selected = set(plan.selected_stages or [])
    hint_stages = set(hint.selected_stages or [])
    if not selected or not selected.issubset(hint_stages | selected):
        return plan
    # Carry over methods for stages that match
    methods: dict[str, str] = {}
    for stage, method in hint.selected_methods.items():
        if stage in selected or stage in (plan.resolved_execution_order or []):
            methods[stage] = method
    if methods:
        return plan.model_copy(update={"selected_methods": methods})
    return plan


def apply_set_params(plan: AskPlanState, set_params: list[str]) -> AskPlanState:
    """Apply --set params to a plan. Public for use in non-interactive mode."""
    return _apply_set_params(plan, set_params)


def parse_set_params(raw: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise ValueError(f"--set value must be KEY=VALUE, got: {item!r}")
        key, _, value = item.partition("=")
        result[key.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# Rendering (confirm screen only — other steps use questionary directly)
# ---------------------------------------------------------------------------

_ROLE_LABELS = {
    "executable": "run",
    "dependency": "dep",
    "planned_only": "plan-only",
}

_GENOME_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmg]?)$", re.IGNORECASE)
_GENOME_SIZE_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}


def _render_confirm(req: AskClarificationRequest) -> None:
    summary = req.summary or {}
    print()
    print("=" * 50)
    print("Run Plan")
    print("=" * 50)

    print(f"\nSession:  {summary.get('session_id', '?')}")
    print(f"Data dir: {summary.get('data_dir', '?')}")
    print(f"Manifest: {summary.get('manifest_ref', '?')}")
    source_paths = summary.get("source_paths")
    if source_paths:
        print(f"Input:    {', '.join(source_paths)}")

    roles = summary.get("stage_roles", {})
    order = summary.get("resolved_execution_order", [])
    if order:
        print("\nStages:")
        for stage in order:
            role = roles.get(stage, "")
            label = _ROLE_LABELS.get(role, role)
            print(f"  [{label:>9}] {stage}")

    methods = summary.get("selected_methods", {})
    if methods:
        print("\nMethods:")
        for stage, method in methods.items():
            print(f"  {stage}: {method}")

    params = summary.get("method_parameters", {})
    if params:
        print("\nParameters:")
        for method, pdict in params.items():
            for k, v in pdict.items():
                print(f"  {k}: {v}")

    warnings = summary.get("warnings", [])
    for w in warnings:
        print(f"\n  warning: {w}")

    preview_error = summary.get("run_config_preview_error")
    if preview_error:
        print(f"\nRun-config error: {preview_error}")

    print("=" * 50)


# ---------------------------------------------------------------------------
# Prompting — questionary with plain-text fallback
# ---------------------------------------------------------------------------

def _prompt_for_answer(req: AskClarificationRequest) -> AskClarificationResponse:
    if req.kind == "stage_selection":
        return _prompt_stage_selection(req)
    if req.kind == "method_selection":
        return _prompt_method_selection(req)
    if req.kind == "parameter_values":
        return _prompt_parameter_values(req)
    raise ValueError(f"unexpected clarification kind: {req.kind}")


def _prompt_confirm(message: str) -> bool:
    if _HAS_QUESTIONARY:
        return questionary.confirm(message, default=False).ask() or False
    raw = input(f"{message} [y/N] ").strip().lower()
    return raw in ("y", "yes")


def _confirm_auto_resolve(plan: AskPlanState) -> bool:
    stages = ", ".join(plan.selected_stages or [])
    methods = ""
    if plan.selected_methods:
        methods = ", ".join(plan.selected_methods.values())
    deps = [s for s in (plan.resolved_execution_order or []) if s not in (plan.selected_stages or [])]
    dep_str = f" (deps: {', '.join(deps)})" if deps else ""

    print(f"\nAuto-detected: {stages}{dep_str}")
    if methods:
        print(f"  Method: {methods}")
    return _prompt_confirm("Accept?")


def _prompt_stage_selection(req: AskClarificationRequest) -> AskClarificationResponse:
    available = req.choices.get("available_stages", [])
    suggested = req.choices.get("suggested_stages", [])

    if _HAS_QUESTIONARY:
        choices = [
            questionary.Choice(stage, value=stage, checked=(stage in suggested))
            for stage in available
        ]
        selected = questionary.checkbox(
            "Select stages (Space=toggle, Enter=confirm):",
            choices=choices,
        ).ask()
        if not selected:
            selected = suggested
        return AskClarificationResponse(kind="stage_selection", selected_stages=selected)

    # Plain-text fallback
    print()
    print("Available stages:")
    for i, stage in enumerate(available, 1):
        marker = "*" if stage in suggested else " "
        print(f"  {marker} {i}. {stage}")
    if suggested:
        print(f"\nSuggested: {', '.join(suggested)}")
    print("Enter numbers or names, e.g. 1,3 or read_qc,assembly (Enter = accept suggested):")
    raw = input("> ").strip()
    stages = _parse_selection(raw, available) if raw else suggested
    return AskClarificationResponse(kind="stage_selection", selected_stages=stages)


def _prompt_method_selection(req: AskClarificationRequest) -> AskClarificationResponse:
    methods_by_stage = req.choices.get("methods_by_stage", {})
    selected: dict[str, str] = {}

    for stage, methods in methods_by_stage.items():
        method_names = [m["method_name"] for m in methods]
        executable = [m["method_name"] for m in methods if m.get("execution_support") == "executable"]
        default_method = executable[0] if executable else method_names[0] if method_names else None

        if _HAS_QUESTIONARY:
            choices = []
            for m in methods:
                support = m.get("execution_support", "")
                suffix = " (executable)" if support == "executable" else " (planned only)"
                choices.append(questionary.Choice(
                    f"{m['method_name']}{suffix}",
                    value=m["method_name"],
                ))
            answer = questionary.select(
                f"Method for {stage}:",
                choices=choices,
                default=default_method,
            ).ask()
            selected[stage] = answer or default_method or method_names[0]
        else:
            print(f"\nMethods for {stage}:")
            for i, m in enumerate(methods, 1):
                support = m.get("execution_support", "")
                label = " (executable)" if support == "executable" else " (planned only)"
                print(f"  {i}. {m['method_name']}{label}")
            print("Enter method name or number (Enter = first executable):")
            raw = input("> ").strip()
            if not raw and default_method:
                selected[stage] = default_method
            else:
                picks = _parse_selection(raw, method_names)
                selected[stage] = picks[0] if picks else default_method or method_names[0]

    return AskClarificationResponse(kind="method_selection", selected_methods=selected)


def _prompt_parameter_values(req: AskClarificationRequest) -> AskClarificationResponse:
    defaults = req.choices.get("defaults", {})
    params: dict[str, dict[str, Any]] = {}
    print()
    for field in req.required_fields:
        method_key, _, param_name = field.rpartition(".")
        if not method_key:
            method_key = param_name
        default = _find_default(field, defaults)

        if _HAS_QUESTIONARY:
            answer = questionary.text(
                f"{param_name}:",
                default=str(default) if default is not None else "",
            ).ask() or ""
        else:
            if default is not None:
                answer = input(f"{param_name} [{default}]: ").strip()
            else:
                answer = input(f"{param_name}: ").strip()

        value: Any = answer if answer else default
        if param_name == "estimated_genome_size" and isinstance(value, str):
            value = _parse_genome_size(value)
        params.setdefault(method_key, {})[param_name] = value
    return AskClarificationResponse(kind="parameter_values", method_parameters=params)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_selection(raw: str, options: list[str]) -> list[str]:
    normalized = raw.replace("，", ",").replace("、", ",").replace(" ", ",")
    items = [s.strip() for s in normalized.split(",") if s.strip()]
    result: list[str] = []
    for item in items:
        if item.isdigit():
            idx = int(item) - 1
            if 0 <= idx < len(options):
                result.append(options[idx])
        else:
            for opt in options:
                if opt == item or opt.endswith(f".{item}"):
                    result.append(opt)
                    break
            else:
                result.append(item)
    return result


def _find_default(field: str, defaults: dict[str, Any]) -> Any:
    for method_key, pdict in defaults.items():
        if isinstance(pdict, dict):
            param = field.rsplit(".", 1)[-1]
            if param in pdict:
                return pdict[param]
    return None


def _parse_genome_size(raw: str) -> int | str:
    m = _GENOME_SIZE_RE.match(raw.strip())
    if not m:
        return raw
    number = float(m.group(1))
    suffix = m.group(2).lower()
    multiplier = _GENOME_SIZE_MULTIPLIERS.get(suffix, 1)
    return int(number * multiplier)


def _apply_set_params(plan: AskPlanState, set_params: list[str]) -> AskPlanState:
    if not set_params:
        return plan
    parsed = parse_set_params(set_params)
    # Assembly/Flye MVP only. Generalize via method descriptors when more
    # executable methods are supported.
    _PARAM_ALIASES = {
        "genome_size": ("assembly.candidate.flye", "estimated_genome_size"),
        "estimated_genome_size": ("assembly.candidate.flye", "estimated_genome_size"),
        "read_type": ("assembly.candidate.flye", "read_type"),
        "threads": ("assembly.candidate.flye", "threads"),
    }
    existing = dict(plan.method_parameters or {})
    for key, value in parsed.items():
        alias = _PARAM_ALIASES.get(key)
        if alias:
            method, param = alias
            bucket = dict(existing.get(method, {}))
            if param == "estimated_genome_size":
                value = _parse_genome_size(value)
            bucket[param] = value
            existing[method] = bucket
    if existing:
        return plan.model_copy(update={"method_parameters": existing})
    return plan


def _execute_confirmed_plan(plan: AskPlanState, args: argparse.Namespace) -> int:
    from app.entrypoints.ask_execution import execute_confirmed_plan
    return execute_confirmed_plan(plan, args)
