"""Interactive TTY loop for `gene-agent ask`."""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from pydantic import ValidationError

from app.entrypoints.ask_planner import (
    READ_QC_FILTER_METHODS,
    READ_QC_FILTER_SKIP,
    READ_QC_HANDOFF_METHOD,
    READ_QC_STATS_METHOD,
    auto_resolve_plan,
    merge_clarification_response,
    next_clarification,
    save_plan_to_default_path,
)
from app.schemas.ask_plan import AskClarificationRequest, AskClarificationResponse, AskPlanState
from app.schemas.shared_parameters import FLYE_TO_SHARED_READ_TYPE, SHARED_TO_METHOD_READ_TYPE, SharedParameters

try:
    import questionary
    _HAS_QUESTIONARY = True
except ImportError:
    _HAS_QUESTIONARY = False


class _UserAbort(Exception):
    pass


def is_interactive(args: argparse.Namespace) -> bool:
    if getattr(args, "json", False):
        return False
    if getattr(args, "non_interactive", False):
        return False
    return sys.stdin.isatty()


def run_interactive_loop(plan: AskPlanState, args: argparse.Namespace) -> int:
    try:
        return _interactive_loop_inner(plan, args)
    except (_UserAbort, KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 1


def _interactive_loop_inner(plan: AskPlanState, args: argparse.Namespace) -> int:
    original_plan = plan
    auto_resolved = auto_resolve_plan(plan)
    plan = _apply_set_params(auto_resolved, getattr(args, "set_params", None) or [])

    _auto_hint: AskPlanState | None = None

    if plan.selected_stages is not None and original_plan.selected_stages is None:
        if _confirm_auto_resolve(plan):
            _auto_hint = plan
        else:
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
                print("\n--- Restarting from stage selection ---\n")
                plan = plan.model_copy(update={
                    "selected_stages": None,
                    "resolved_execution_order": None,
                    "selected_methods": None,
                    "shared_parameters": None,
                    "method_parameters": None,
                    "confirmed": False,
                })
                plan_ref = save_plan_to_default_path(plan)
                continue
            response = AskClarificationResponse(kind="confirm", confirmed=True)
        else:
            response = _prompt_for_answer(clarification)

        plan = merge_clarification_response(plan, response)

        if response.kind == "stage_selection" and _auto_hint is not None:
            plan = _try_auto_fill_methods(plan, _auto_hint)

        plan_ref = save_plan_to_default_path(plan)

    return _execute_confirmed_plan(plan, args)


def _try_auto_fill_methods(plan: AskPlanState, hint: AskPlanState) -> AskPlanState:
    """If user-selected stages match the auto-resolved hint, carry over methods."""
    if not hint.selected_methods:
        return plan
    existing = dict(plan.selected_methods or {})
    for stage, method in hint.selected_methods.items():
        if stage not in existing and stage in (plan.resolved_execution_order or []):
            existing[stage] = method
    if existing != (plan.selected_methods or {}):
        return plan.model_copy(update={"selected_methods": existing})
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
# Rendering
# ---------------------------------------------------------------------------

_ROLE_LABELS = {
    "executable": "run",
    "dependency": "dep",
    "planned_only": "plan-only",
}

_PARAM_HINTS: dict[str, str] = {
    "assembly_review.eval.busco.lineage_path": "path to BUSCO lineage dir, e.g. /db/busco/insecta_odb10",
    "assembly_review.eval.busco.lineage_name": "auto-derived from path",
    "assembly_review.eval.busco.lineage_version": "e.g. odb10; auto-detected from name",
    "assembly_review.eval.busco.lineage_source": "local / busco_downloads",
    "assembly_review.eval.busco.threads": "CPU threads for BUSCO",
    "assembly_review.eval.quast.threads": "CPU threads for QUAST",
    "assembly.candidate.flye.threads": "CPU threads for Flye assembler",
    "estimated_genome_size": "e.g. 500m, 2.5g",
    "read_type": "sequencing platform read type",
    "genome_survey.kmer_count.jellyfish.kmer_size": "k-mer length, typically 21",
    "genome_survey.kmer_count.jellyfish.hash_size": "hash table size, e.g. 1G",
    "genome_survey.kmer_count.jellyfish.threads": "CPU threads for jellyfish",
    "genome_survey.kmer_count.jellyfish.read_length": "average read length in bp",
    "hic_scaffolding.candidate.yahs.threads": "CPU threads for YaHS",
    "repeat_annotation.masking.repeatmasker.threads": "CPU threads for RepeatMasker",
    "read_qc.length_filter.seqkit_seq.min_len": "minimum read length to keep (bp)",
}

_GENOME_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmg]?)$", re.IGNORECASE)
_GENOME_SIZE_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}
_SHARED_READ_TYPE_VALUES = tuple(SHARED_TO_METHOD_READ_TYPE.keys())

_STAGE_DESCRIPTIONS: dict[str, str] = {
    "read_qc": "quality stats and optional length filtering",
    "genome_survey": "k-mer profiling and genome size estimation",
    "assembly": "de novo genome assembly",
    "assembly_review": "QUAST/BUSCO quality evaluation",
    "hic_scaffolding": "Hi-C based chromosome scaffolding",
    "repeat_annotation": "repeat element identification and masking",
    "gene_annotation": "gene structure prediction",
    "function_annotation": "protein function annotation",
}


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
    exec_status = summary.get("method_execution_status", {})
    if methods:
        print("\nMethods:")
        for stage, value in methods.items():
            method_list = value if isinstance(value, list) else [value]
            for m in method_list:
                status = exec_status.get(m, "")
                tag = " [local]" if status == "executable" else " [deferred]" if status == "planned_only" else ""
                print(f"  {stage}: {m}{tag}")

    shared = summary.get("shared_parameters", {})
    if shared:
        print("\nShared parameters:")
        for k, v in shared.items():
            print(f"  {k}: {v}")

    params = summary.get("method_parameters", {})
    if params:
        print("\nParameters:")
        for method, pdict in params.items():
            short = method.rsplit(".", 1)[-1]
            for k, v in pdict.items():
                print(f"  [{short}] {k}: {v}")

    warnings = summary.get("warnings", [])
    for w in warnings:
        print(f"\n  warning: {w}")

    preview_error = summary.get("run_config_preview_error")
    if preview_error:
        print(f"\nRun-config error: {preview_error}")

    print("=" * 50)


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

def _safe_ask(question: Any) -> Any:
    """Call questionary .ask() with Ctrl+C / Esc handling."""
    try:
        result = question.ask()
    except KeyboardInterrupt:
        raise _UserAbort()
    if result is None:
        raise _UserAbort()
    return result


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
        return _safe_ask(questionary.confirm(message, default=False))
    raw = input(f"{message} [y/N] ").strip().lower()
    return raw in ("y", "yes")


def _confirm_auto_resolve(plan: AskPlanState) -> bool:
    stages = ", ".join(plan.selected_stages or [])
    methods = ""
    if plan.selected_methods:
        all_methods = []
        for v in plan.selected_methods.values():
            if isinstance(v, list):
                all_methods.extend(v)
            else:
                all_methods.append(v)
        methods = ", ".join(all_methods)
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
        print("\n--- Step 1: Stage Selection ---")
        print("  Recommended stages are pre-checked based on your input data.\n")
        choices = [
            questionary.Choice(
                f"{stage} — {_STAGE_DESCRIPTIONS.get(stage, '')}",
                value=stage,
                checked=(stage in suggested),
            )
            for stage in available
        ]
        selected = _safe_ask(questionary.checkbox(
            "Select stages:",
            choices=choices,
            instruction="(Space toggle, Enter confirm)",
        ))
        if not selected:
            selected = suggested
        return AskClarificationResponse(kind="stage_selection", selected_stages=selected)

    # Plain-text fallback
    print("\n--- Step 1: Stage Selection ---")
    print("  * = recommended based on your input data\n")
    print("Available stages:")
    for i, stage in enumerate(available, 1):
        marker = "*" if stage in suggested else " "
        desc = _STAGE_DESCRIPTIONS.get(stage, "")
        desc_str = f"  {desc}" if desc else ""
        print(f"  {marker} {i}. {stage}{desc_str}")
    if suggested:
        print(f"\n  Recommended: {', '.join(suggested)}")
    print("\n  Enter numbers or names, e.g. 1,3 or read_qc,assembly")
    print("  Press Enter to accept recommended.")
    raw = input("> ").strip()
    stages = _parse_selection(raw, available) if raw else suggested
    return AskClarificationResponse(kind="stage_selection", selected_stages=stages)


def _prompt_method_selection(req: AskClarificationRequest) -> AskClarificationResponse:
    methods_by_stage = req.choices.get("methods_by_stage", {})
    selected: dict[str, str | list[str]] = {}

    print("\n--- Step 2: Method Selection ---")
    print("  [local] = runs on this machine  [deferred] = planned for HPC/cloud\n")

    for stage, methods in methods_by_stage.items():
        method_names = [m["method_name"] for m in methods]
        if stage == "read_qc":
            selected[stage] = _prompt_read_qc_filter_choice(methods, req.choices.get("defaults", {}).get(stage))
            continue
        executable = [m["method_name"] for m in methods if m.get("execution_support") == "executable"]
        default_checked = executable if executable else method_names[:1]

        if _HAS_QUESTIONARY:
            choices = []
            for m in methods:
                support = m.get("execution_support", "")
                suffix = " [local]" if support == "executable" else " [deferred]"
                choices.append(questionary.Choice(
                    f"{m['method_name']}{suffix}",
                    value=m["method_name"],
                    checked=(m["method_name"] in default_checked),
                ))
            answer = _safe_ask(questionary.checkbox(
                f"Methods for {stage}:",
                choices=choices,
                instruction="(Space toggle, Enter confirm)",
            ))
            if not answer:
                answer = default_checked
            selected[stage] = answer[0] if len(answer) == 1 else answer
        else:
            print(f"  Methods for [{stage}]:")
            for i, m in enumerate(methods, 1):
                support = m.get("execution_support", "")
                label = " [local]" if support == "executable" else " [deferred]"
                marker = "*" if m["method_name"] in default_checked else " "
                print(f"    {marker} {i}. {m['method_name']}{label}")
            print(f"\n  Enter numbers or names. Enter = {', '.join(default_checked)}:")
            raw = input(f"  {stage}> ").strip()
            if not raw:
                picks = default_checked
            else:
                picks = _parse_selection(raw, method_names)
            selected[stage] = picks[0] if len(picks) == 1 else picks

    return AskClarificationResponse(kind="method_selection", selected_methods=selected)


def _prompt_read_qc_filter_choice(methods: list[dict[str, Any]], default: str | list[str] | None) -> list[str]:
    default_methods = default if isinstance(default, list) else [default] if default else []
    default_filter = next((method for method in default_methods if method in READ_QC_FILTER_METHODS), READ_QC_FILTER_SKIP)
    filters = [m for m in methods if m.get("method_name") in READ_QC_FILTER_METHODS]
    filter_names = [m["method_name"] for m in filters]

    if _HAS_QUESTIONARY:
        choices = [
            questionary.Choice(
                m.get("display_name") or m["method_name"],
                value=m["method_name"],
            )
            for m in filters
        ]
        answer = _safe_ask(questionary.select(
            "Read QC length filtering:",
            choices=choices,
            default=default_filter,
            instruction="(↑↓ navigate, Enter confirm)",
        ))
        filter_method = answer or default_filter
    else:
        print("  Methods for [read_qc]:")
        print(f"    * always: {READ_QC_STATS_METHOD}")
        print("    length filtering:")
        for i, m in enumerate(filters, 1):
            label = m.get("display_name") or m["method_name"]
            marker = "*" if m["method_name"] == default_filter else " "
            print(f"    {marker} {i}. {label}")
        print(f"    * always: {READ_QC_HANDOFF_METHOD}")
        print(f"\n  Choose one length filter. Enter = {default_filter}:")
        raw = input("  read_qc length_filter> ").strip()
        picks = _parse_selection(raw, filter_names) if raw else [default_filter]
        filter_method = picks[0] if picks else default_filter

    return [READ_QC_STATS_METHOD, filter_method, READ_QC_HANDOFF_METHOD]


def _prompt_parameter_values(req: AskClarificationRequest) -> AskClarificationResponse:
    defaults = req.choices.get("defaults", {})
    shared_defaults = req.choices.get("shared_defaults", {})
    params: dict[str, dict[str, Any]] = {}
    shared_params: dict[str, Any] = {}
    print("\n--- Step 3: Parameters ---")
    print("  Press Enter to accept [default], or type a new value.\n")
    for field in req.required_fields:
        method_key, _, param_name = field.rpartition(".")
        if not method_key:
            method_key = param_name
        short_method = method_key.rsplit(".", 1)[-1]
        stage_name = method_key.split(".")[0] if "." in method_key else method_key
        label = f"[{stage_name} / {short_method}] {param_name}"

        hint = _PARAM_HINTS.get(f"{method_key}.{param_name}") or _PARAM_HINTS.get(param_name)
        if hint:
            label = f"{label}  ({hint})"

        default = shared_defaults.get(param_name) if method_key == "shared_parameters" else _find_default(field, defaults)

        if method_key == "shared_parameters":
            value = _prompt_shared_parameter(label, param_name, default)
            shared_params[param_name] = value
        else:
            value = _prompt_text_value(label, default)
            if param_name == "estimated_genome_size" and isinstance(value, str):
                value = _parse_genome_size(value)
            params.setdefault(method_key, {})[param_name] = value
            _update_derived_defaults(method_key, param_name, value, defaults)
    return AskClarificationResponse(
        kind="parameter_values",
        shared_parameters=shared_params or None,
        method_parameters=params or None,
    )


def _prompt_text_value(label: str, default: Any) -> Any:
    if _HAS_QUESTIONARY:
        answer = _safe_ask(questionary.text(
            f"{label}:",
            default=str(default) if default is not None else "",
        ))
    else:
        if default is not None:
            answer = input(f"  {label} [{default}]: ").strip()
        else:
            answer = input(f"  {label}: ").strip()
    return answer if answer else default


def _prompt_shared_parameter(label: str, param_name: str, default: Any) -> Any:
    if param_name == "read_type":
        return _prompt_shared_read_type(label, default)

    while True:
        value = _prompt_text_value(label, default)
        if param_name == "estimated_genome_size" and isinstance(value, str):
            value = _parse_genome_size(value)
        if _shared_parameter_value_is_valid(param_name, value):
            return value


def _prompt_shared_read_type(label: str, default: Any) -> Any:
    default_value = _normalize_shared_read_type(default)
    if _HAS_QUESTIONARY:
        return _safe_ask(questionary.select(
            f"{label}:",
            choices=[
                questionary.Choice(_read_type_choice_label(value), value=value)
                for value in _SHARED_READ_TYPE_VALUES
            ],
            default=default_value if default_value in _SHARED_READ_TYPE_VALUES else None,
            instruction="(use normalized shared read type)",
        ))

    alias_text = ", ".join(f"{alias}->{value}" for alias, value in sorted(FLYE_TO_SHARED_READ_TYPE.items()))
    print(f"    accepted: {', '.join(_SHARED_READ_TYPE_VALUES)}")
    print(f"    Flye aliases accepted: {alias_text}")
    while True:
        value = _prompt_text_value(label, default_value)
        normalized = _normalize_shared_read_type(value)
        if _shared_parameter_value_is_valid("read_type", normalized):
            return normalized


def _read_type_choice_label(value: str) -> str:
    flye_aliases = sorted(alias for alias, normalized in FLYE_TO_SHARED_READ_TYPE.items() if normalized == value)
    suffix = f" (Flye: {', '.join(flye_aliases)})" if flye_aliases else ""
    return f"{value}{suffix}"


def _normalize_shared_read_type(value: Any) -> Any:
    if value is None:
        return None
    raw = str(value).strip()
    return FLYE_TO_SHARED_READ_TYPE.get(raw, raw)


def _shared_parameter_value_is_valid(param_name: str, value: Any) -> bool:
    try:
        SharedParameters.model_validate({param_name: value})
    except ValidationError as exc:
        print(f"  Invalid {param_name}: {exc.errors()[0]['msg']}")
        return False
    return True


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


def _update_derived_defaults(method_key: str, param_name: str, value: Any, defaults: dict[str, Any]) -> None:
    """After user provides a value, derive related defaults (e.g. BUSCO lineage fields from path)."""
    if method_key == "assembly_review.eval.busco" and param_name == "lineage_path" and value:
        from pathlib import Path as _Path
        name = _Path(str(value)).name
        bucket = defaults.setdefault(method_key, {})
        bucket.setdefault("lineage_name", name)
        import re
        m = re.search(r"(odb\d+)", name)
        if m:
            bucket.setdefault("lineage_version", m.group(1))
        bucket.setdefault("lineage_source", "local")


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
        "genome_size": ("shared_parameters", "estimated_genome_size"),
        "estimated_genome_size": ("shared_parameters", "estimated_genome_size"),
        "read_type": ("shared_parameters", "read_type"),
        "threads": ("assembly.candidate.flye", "threads"),
    }
    existing = dict(plan.method_parameters or {})
    shared = plan.shared_parameters.model_dump(exclude_none=True) if plan.shared_parameters else {}
    for key, value in parsed.items():
        alias = _PARAM_ALIASES.get(key)
        if alias:
            method, param = alias
            if method == "shared_parameters":
                if param == "estimated_genome_size":
                    value = _parse_genome_size(value)
                if param == "read_type":
                    value = FLYE_TO_SHARED_READ_TYPE.get(str(value), value)
                shared[param] = value
                continue
            bucket = dict(existing.get(method, {}))
            bucket[param] = value
            existing[method] = bucket
    update = {}
    if existing:
        update["method_parameters"] = existing
    if shared:
        update["shared_parameters"] = SharedParameters.model_validate(shared)
    if update:
        return plan.model_copy(update=update)
    return plan


def _execute_confirmed_plan(plan: AskPlanState, args: argparse.Namespace) -> int:
    from app.entrypoints.ask_execution import execute_confirmed_plan
    return execute_confirmed_plan(plan, args)
