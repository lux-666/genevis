"""Conversational sample intake, planning, and execution for Gene Agent.

The TUI owns mutable per-run facts.  It does not inspect or edit machine
configuration: the only machine-state operation is a read-only dependency
snapshot created as part of the final immutable freeze transaction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal, Mapping, cast

from app.schemas.tui_intake import RecommendationPolicy


def run_tui(data_dir: str, *, resume_session: str | None = None) -> int:
    """Launch a new conversational run or route one durable TUI session."""

    try:
        if resume_session:
            return _resume_tui_session(data_dir, resume_session)
        return _tui_main(data_dir)
    except (KeyboardInterrupt, EOFError):
        print("\nAborted during interactive planning.")
        return 1


def _tui_main(data_dir: str) -> int:
    """Run the persistent top-level TUI prompt.

    Completing or resuming one session returns here.  The only command handled
    at this level is ``/resume``; every other non-empty input starts a new
    immutable-run draft.
    """

    print("\n=== gene-agent interactive ===\n")
    handled_session = False
    while True:
        print(
            "请描述本次运行目标（例如："
            "\u201c我想对 T2T-CHM13 做 T2T 级组装\u201d或"
            "\u201c以 Solea ovata 为目标物种进行染色体级组装\u201d；"
            "目标物种必须使用完整拉丁学名）。"
        )
        print("输入 /resume 可继续草稿、处理 review gate 或创建重跑草稿。")
        try:
            request_text = input("> ").strip()
        except EOFError:
            print("\n已退出 gene-agent TUI。")
            return 0 if handled_session else 1
        except KeyboardInterrupt:
            print("\nAborted during request entry.")
            return 1

        if not request_text:
            print("error: empty request", file=sys.stderr)
            return 0 if handled_session else 1
        if request_text == "/resume":
            session_id = _select_resume_session(data_dir)
            if session_id is None:
                continue
            handled_session = True
            _resume_tui_session(data_dir, session_id)
            print()
            continue
        if request_text.startswith("/"):
            print("未知命令；当前只支持 /resume。\n")
            continue
        handled_session = True
        _start_tui_session(data_dir, request_text)
        print()


def _start_tui_session(data_dir: str, request_text: str) -> int:
    from app.entrypoints.run import generate_session_id
    from app.entrypoints.tui_intake import (
        apply_asset_answer,
        create_tui_session_draft,
        parse_asset_answer_deterministically,
        parse_workflow_intent,
        save_tui_session_draft,
    )

    print("\n正在解析目标；若 LLM 不可用，将使用固定问题队列。\n")
    intent = parse_workflow_intent(
        request_text,
        llm_parser=_parse_goal_with_configured_llm,
    )
    draft = create_tui_session_draft(
        session_id=generate_session_id(data_dir),
        data_dir=data_dir,
        request_text=request_text,
        intent=intent,
    )
    initial_assets = parse_asset_answer_deterministically(
        request_text,
        focused_role=None,
    )
    if initial_assets.candidates or initial_assets.unresolved_paths:
        result = apply_asset_answer(
            draft,
            answer=request_text,
            focused_role=None,
            llm_parser=_parse_assets_with_configured_llm,
        )
        draft = result.draft
        _render_asset_result(result)
    state_path, _manifest_path = save_tui_session_draft(draft)
    print(f"已创建本次运行草稿：{draft.session_id}")
    print(f"草稿状态：{state_path}\n")
    return _continue_tui_draft(draft)


def _select_resume_session(data_dir: str) -> str | None:
    """List run sessions only and let the operator choose one to resume."""

    runs_dir = Path(data_dir).expanduser().resolve(strict=False) / "runs"
    if not runs_dir.is_dir():
        print("当前没有可恢复的运行。\n")
        return None
    session_ids = sorted(
        (child.name for child in runs_dir.iterdir() if child.is_dir()),
        reverse=True,
    )
    if not session_ids:
        print("当前没有可恢复的运行。\n")
        return None

    print("\n可恢复的 session：")
    for index, session_id in enumerate(session_ids, start=1):
        print(f"  {index}. {session_id}  [{_resume_status(data_dir, session_id)}]")
    raw = input("选择编号或输入 session_id；直接回车取消：").strip()
    if not raw:
        return None
    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(session_ids):
            return session_ids[index - 1]
    if raw in session_ids:
        return raw
    print(f"未找到 session：{raw}\n")
    return None


def _resume_status(data_dir: str, session_id: str) -> str:
    from app.entrypoints.tui_intake import load_tui_session_draft
    from app.storage.frozen_inputs import (
        FrozenInputError,
        has_frozen_input_contract,
        validate_frozen_inputs,
    )
    from app.storage.paths import SessionPaths

    paths = SessionPaths(data_dir, session_id)
    state_path = paths.tui_state_path
    draft = None
    if state_path.exists():
        try:
            draft = load_tui_session_draft(data_dir, session_id)
        except (OSError, ValueError):
            return "invalid draft"
    if not has_frozen_input_contract(paths):
        return f"draft/{draft.lifecycle}" if draft is not None else "incomplete"
    try:
        validate_frozen_inputs(paths)
    except (FrozenInputError, OSError, ValueError):
        return "invalid frozen inputs"
    if draft is None:
        return "frozen structured run"
    return _durable_supervision_state(draft)


def _continue_tui_draft(draft: Any) -> int:
    """Resume one mutable draft from its next deterministic transition."""

    from app.entrypoints.tui_intake import save_tui_session_draft

    if draft.lifecycle == "intake":
        draft = _collect_sample_assets(draft)
        if draft is None:
            return 1
        draft = draft.model_copy(update={"lifecycle": "planning"})
        save_tui_session_draft(draft)

    if draft.lifecycle in {"planning", "ready_to_confirm"}:
        planned = _collect_run_plan(draft)
        if planned is None:
            return 1
        draft, plan = planned
        return _freeze_and_execute(draft, plan)

    print(
        f"error: session {draft.session_id} is not an editable draft "
        f"(lifecycle={draft.lifecycle})",
        file=sys.stderr,
    )
    return 1


def _collect_sample_assets(draft: Any) -> Any | None:
    from app.entrypoints.tui_intake import (
        apply_asset_answer,
        apply_batch_t2t_facts,
        confirm_no_hic_degradation,
        next_intake_clarification,
        save_tui_session_draft,
    )

    while True:
        clarification = next_intake_clarification(draft)
        if clarification is None:
            print("样本资产已补齐。\n")
            return draft

        print(clarification.question)
        try:
            answer = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted during sample intake.")
            return None

        if clarification.kind == "degradation_confirmation":
            confirmed = _is_affirmative(answer)
            if confirmed:
                draft = confirm_no_hic_degradation(draft, confirmed=True)
                save_tui_session_draft(draft)
                print("已确认使用非 Hi-C/contig 路线。\n")
                continue
            intake = draft.intake.model_copy(update={"hic_reads": None})
            draft = draft.model_copy(
                update={"intake": intake, "degradation_decision": None}
            )
            save_tui_session_draft(draft)
            print("未确认降级；请改为提供明确的 Hi-C R1/R2。\n")
            continue

        if clarification.kind == "t2t_batch":
            if not answer:
                print("未收到任何信息，请重新输入。\n")
                continue
            result = apply_batch_t2t_facts(
                draft,
                answer=answer,
                llm_parser=_parse_assets_with_configured_llm,
            )
            if _would_replace_confirmed_asset(result) and _confirm(
                "回答中包含已确认资产的新值，是否替换旧值？", default=False
            ):
                result = apply_batch_t2t_facts(
                    draft,
                    answer=answer,
                    llm_parser=_parse_assets_with_configured_llm,
                    replace_confirmed=True,
                )
            draft = result.draft
            save_tui_session_draft(draft)
            _render_batch_t2t_result(result)
            continue

        if not answer:
            print("未收到路径或明确的\u201c没有 Hi-C\u201d，请重新输入。\n")
            continue

        focused_role = clarification.role
        if focused_role not in {"hifi_reads", "ont_reads", "hic_reads"}:
            raise RuntimeError("asset collection clarification requires an asset role")
        result = apply_asset_answer(
            draft,
            answer=answer,
            focused_role=focused_role,
            llm_parser=_parse_assets_with_configured_llm,
        )
        if _would_replace_confirmed_asset(result) and _confirm(
            "回答中包含已确认资产的新值，是否替换旧值？", default=False
        ):
            result = apply_asset_answer(
                draft,
                answer=answer,
                focused_role=focused_role,
                llm_parser=_parse_assets_with_configured_llm,
                replace_confirmed=True,
            )
        draft = result.draft
        save_tui_session_draft(draft)
        _render_asset_result(result)


def _collect_run_plan(draft: Any) -> tuple[Any, Any] | None:
    """Use the existing planner in memory and persist only TUI draft fields."""

    from app.entrypoints.ask_interactive import (
        _prompt_confirm,
        _prompt_for_answer,
        _render_confirm,
        _review_plan_draft,
        _UserAbort,
    )
    from app.entrypoints.ask_planner import (
        _auto_fill_inferred_defaults,
        merge_clarification_response,
        next_clarification,
    )
    from app.entrypoints.tui_intake import save_tui_session_draft
    from app.schemas.ask_plan import AskClarificationResponse
    from app.storage.paths import SessionPaths

    try:
        plan = _planner_state_from_tui_draft(draft)
        if draft.lifecycle == "planning":
            if plan.selected_stage_names is None:
                print("\n正在根据目标和已确认的样本事实生成 Plan Draft...\n")
                try:
                    plan = _generate_plan_draft_with_configured_llm(plan)
                except Exception as exc:
                    print(
                        "error: LLM 未能生成合法的 Plan Draft；"
                        "本次规划未使用固定规则回退："
                        f"{exc}",
                        file=sys.stderr,
                    )
                    print("请通过 /resume 重试该草稿。", file=sys.stderr)
                    return None
                draft = _copy_planner_fields_to_draft(draft, plan, lifecycle="planning")
                save_tui_session_draft(draft)

            reviewed = _review_plan_draft(plan)
            if reviewed is None:
                draft = _discard_generated_plan_fields(draft)
                save_tui_session_draft(draft)
                print("\n已丢弃生成的 Plan Draft；可通过 /resume 重新生成。")
                return None
            plan = reviewed
            draft = _copy_planner_fields_to_draft(
                draft, plan, lifecycle="ready_to_confirm"
            )
            save_tui_session_draft(draft)

        ref = SessionPaths(draft.data_dir, draft.session_id).tui_state_path
        while True:
            clarification = next_clarification(plan, ask_plan_ref=ref)
            if clarification is None:
                plan = _auto_fill_inferred_defaults(plan)
                draft = _copy_planner_fields_to_draft(
                    draft, plan, lifecycle="ready_to_confirm"
                )
                save_tui_session_draft(draft)
                return draft, plan

            if clarification.kind == "confirm":
                plan = _auto_fill_inferred_defaults(plan)
                _render_confirm(clarification)
                if not _prompt_confirm("确认 manifest 和 run config 并开始执行？"):
                    print("\n返回 Plan Draft 继续编辑。\n")
                    reviewed = _review_plan_draft(
                        plan.model_copy(update={"confirmed": False})
                    )
                    if reviewed is None:
                        draft = _discard_generated_plan_fields(draft)
                        save_tui_session_draft(draft)
                        return None
                    plan = reviewed
                    draft = _copy_planner_fields_to_draft(
                        draft, plan, lifecycle="ready_to_confirm"
                    )
                    save_tui_session_draft(draft)
                    continue
                response = AskClarificationResponse(kind="confirm", confirmed=True)
            else:
                response = _prompt_for_answer(clarification)

            plan = merge_clarification_response(plan, response)
            draft = _copy_planner_fields_to_draft(draft, plan, lifecycle="planning")
            save_tui_session_draft(draft)
    except (_UserAbort, KeyboardInterrupt, EOFError):
        print("\nAborted during run planning.")
        return None


def _planner_state_from_tui_draft(draft: Any) -> Any:
    from app.entrypoints.tui_intake import no_hic_contig_stage_scope
    from app.schemas.ask_plan import AskPlanState
    from app.schemas.tui_intake import NoHiCFact
    from app.storage.paths import SessionPaths

    has_hifi = draft.intake.hifi_reads is not None
    has_ont = draft.intake.ont_reads is not None
    platform: str | None = "pacbio_hifi" if has_hifi and not has_ont else "ont"
    if has_hifi and has_ont:
        platform = None

    degraded = (
        isinstance(draft.intake.hic_reads, NoHiCFact)
        and draft.degradation_decision is not None
        and draft.degradation_decision.confirmed
    )

    method_parameters = (
        {
            method: dict(parameters)
            for method, parameters in (draft.method_parameters or {}).items()
        }
        if draft.method_parameters is not None
        else {}
    )
    # Assembly method selection is runtime-derived.  Keep explicit legacy
    # selected_methods if a durable draft already contains them, but do not
    # synthesize a new method choice from intake intent.
    selected_methods = draft.selected_methods

    requested_stages = list(draft.intent.requested_stages)
    if degraded:
        # New confirmations persist this projection in WorkflowIntent. Keep it
        # here as a compatibility boundary for older durable drafts.
        requested_stages = no_hic_contig_stage_scope(requested_stages)

    entry_request: dict[str, Any] = {
        "command": "run",
        "session_id": draft.session_id,
        "data_dir": draft.data_dir,
        "platform": platform,
        "target_species": draft.intent.target_species,
        "requested_stages": requested_stages,
        "source_paths": [],
        "intent_parse_source": "llm_plan_draft",
        "intake_goal": {
            "target_assembly_level": (
                "contig" if degraded else draft.intent.target_assembly_level
            ),
        },
        "recommendation_policy": draft.recommendation_policy.model_dump(),
    }
    return AskPlanState(
        session_id=draft.session_id,
        data_dir=draft.data_dir,
        request_text=draft.request_text,
        entry_request=entry_request,
        manifest_ref=str(
            SessionPaths(draft.data_dir, draft.session_id).manifest_draft_path
        ),
        selected_stage_occurrences=draft.selected_stage_occurrences,
        selected_methods=selected_methods,
        shared_parameters=draft.shared_parameters,
        method_parameters=method_parameters or None,
        stage_parameters=draft.stage_parameters,
        confirmed=False,
    )


def _generate_plan_draft_with_configured_llm(plan: Any) -> Any:
    """Generate one editable AskPlanState draft from goal plus confirmed intake."""

    context = _plan_draft_context(plan)
    system = (
        "You create one editable Gene Agent Plan Draft. Return exactly one JSON "
        "object with selected_stage_occurrences and optional shared_parameters, "
        "method_parameters, and stage_parameters. Use only "
        "the stages and planning-time parameter names supplied in "
        "the context. selected_stage_occurrences must contain the complete ordered "
        "workflow JSON list, which may be empty for an Inventory-only run; each "
        "non-empty item contains stage and allow_local_execution. "
        "needed for every stated goal: combine multiple goals instead of stopping "
        "at the first one. Preserve the literal order and duplicate occurrences "
        "given in requested_stage_scope. When requested_stage_scope is non-empty, "
        "it is a hard ceiling: do not add any other stage. For a T2T goal, use "
        "canonical_t2t_workflow from the context. Set allow_local_execution=false "
        "for every stage listed in manual_only_stages because those stages have "
        "no local executor. "
        "Do not choose methods in this draft. The system fills fixed-stage "
        "defaults (read_qc stats plus skip, Jellyfish plus GenomeScope2, "
        "Purge_dups, QUAST plus BUSCO, AutoHiC, and minimap2); Assembly stays "
        "deferred until upstream evidence is available and must not receive "
        "method parameters in this draft. Fill only parameters that are explicit "
        "in the request/intake; do not invent ordinary planning defaults. Never "
        "invent paths, database provenance, species facts, genome size, or review "
        "decisions. Do not fill runtime-derived catalog L2 parameters; omit them "
        "so the existing UI can show them as deferred. Do not return Assembly "
        "method parameters. Hi-C presence alone must "
        "not enable hifiasm Hi-C phasing, and ONT presence alone must not enable "
        "hifiasm ultra-long input. Choose allow_local_execution independently for "
        "every occurrence; it is explicit operator authorization, not a method "
        "selection consequence. Do not return confirmation fields."
    )
    payload = _invoke_json_model(
        system,
        json.dumps(context, ensure_ascii=False, indent=2, default=str),
    )
    return _apply_llm_plan_draft(plan, payload)


def _plan_draft_context(plan: Any) -> dict[str, Any]:
    from app.entrypoints.ask_planner import _all_selectable_stages, _load_manifest
    from app.workflow import MANUAL_ONLY_STAGES, recommended_workflow_template

    requested_scope = [
        str(stage)
        for stage in (plan.entry_request or {}).get("requested_stages", [])
        if str(stage) in _all_selectable_stages()
    ]
    available_stages = requested_scope or _all_selectable_stages()

    manifest = _load_manifest(plan.manifest_ref)
    return {
        "original_request": plan.request_text,
        "intake_goal_facts": (plan.entry_request or {}).get("intake_goal") or {},
        "confirmed_manifest": (
            manifest.model_dump(mode="json", exclude_none=True)
            if manifest is not None
            else None
        ),
        "existing_shared_parameters": (
            plan.shared_parameters.model_dump(mode="json", exclude_none=True)
            if plan.shared_parameters is not None
            else {}
        ),
        "available_stages_in_order": available_stages,
        "requested_stage_scope": requested_scope,
        "canonical_t2t_workflow": list(recommended_workflow_template("t2t")),
        "manual_only_stages": sorted(MANUAL_ONLY_STAGES),
        "method_selection": "system_defaults_and_runtime_recommendation",
    }


def _apply_llm_plan_draft(plan: Any, payload: Mapping[str, Any]) -> Any:
    from app.entrypoints.ask_planner import (
        _all_selectable_stages,
        _auto_fill_inferred_defaults,
        _fill_draft_default_methods,
        merge_clarification_response,
    )
    from app.schemas.ask_plan import AskClarificationResponse, AskPlanState
    from app.schemas.shared_parameters import SharedParameters

    allowed_top_level = {
        "selected_stage_occurrences",
        "selected_methods",
        "shared_parameters",
        "method_parameters",
        "stage_parameters",
    }
    unknown = set(payload).difference(allowed_top_level)
    if unknown:
        raise ValueError(f"Plan Draft contains unknown fields: {sorted(unknown)}")

    raw_occurrences = payload.get("selected_stage_occurrences")
    if not isinstance(raw_occurrences, list):
        raise ValueError("Plan Draft selected_stage_occurrences must be a JSON list")
    from app.schemas.ask_plan import PlanStageOccurrence

    occurrences = [
        PlanStageOccurrence.model_validate(occurrence) for occurrence in raw_occurrences
    ]
    stages = [occurrence.stage for occurrence in occurrences]
    invalid_stages = set(stages).difference(_all_selectable_stages())
    if invalid_stages:
        raise ValueError(
            f"Plan Draft contains unknown stages: {sorted(invalid_stages)}"
        )
    requested_scope = [
        str(stage) for stage in (plan.entry_request or {}).get("requested_stages", [])
    ]
    if requested_scope and stages != requested_scope:
        raise ValueError(
            "Plan Draft selected_stage_occurrences must preserve the explicit intent "
            "sequence, including order and repeated occurrences"
        )

    candidate = merge_clarification_response(
        plan,
        AskClarificationResponse(
            kind="stage_selection", selected_stage_occurrences=occurrences
        ),
    )
    candidate = _fill_draft_default_methods(candidate)

    raw_methods = payload.get("selected_methods") or {}
    if not isinstance(raw_methods, Mapping):
        raise ValueError("Plan Draft selected_methods must be an object")
    methods = {str(stage): value for stage, value in raw_methods.items()}
    if methods:
        raise ValueError(
            "Plan Draft must not choose methods; fixed methods are system defaults "
            "and Assembly is recommended at stage time"
        )

    raw_shared = payload.get("shared_parameters") or {}
    raw_method_parameters = payload.get("method_parameters") or {}
    raw_stage_parameters = payload.get("stage_parameters") or {}
    if not isinstance(raw_shared, Mapping):
        raise ValueError("Plan Draft shared_parameters must be an object")
    if not isinstance(raw_method_parameters, Mapping):
        raise ValueError("Plan Draft method_parameters must be an object")
    if not isinstance(raw_stage_parameters, Mapping):
        raise ValueError("Plan Draft stage_parameters must be an object")

    method_parameters = {
        str(method): dict(values)
        for method, values in raw_method_parameters.items()
        if isinstance(values, Mapping)
    }
    if len(method_parameters) != len(raw_method_parameters):
        raise ValueError("Every Plan Draft method parameter block must be an object")
    stage_parameters = {
        str(stage): dict(values)
        for stage, values in raw_stage_parameters.items()
        if isinstance(values, Mapping)
    }
    if len(stage_parameters) != len(raw_stage_parameters):
        raise ValueError("Every Plan Draft stage parameter block must be an object")
    _validate_llm_plan_parameters(candidate, method_parameters, stage_parameters)

    shared_parameters = (
        SharedParameters.model_validate(dict(raw_shared)) if raw_shared else None
    )
    if shared_parameters or method_parameters or stage_parameters:
        candidate = merge_clarification_response(
            candidate,
            AskClarificationResponse(
                kind="parameter_values",
                shared_parameters=shared_parameters,
                method_parameters=method_parameters or None,
                stage_parameters=stage_parameters or None,
            ),
        )
    candidate = _fill_draft_default_methods(candidate)
    candidate = _auto_fill_inferred_defaults(candidate)
    return AskPlanState.model_validate(candidate.model_dump(mode="python"))


def _validate_llm_plan_parameters(
    plan: Any,
    method_parameters: Mapping[str, Mapping[str, Any]],
    stage_parameters: Mapping[str, Mapping[str, Any]],
) -> None:
    from app.entrypoints.planner_catalog import load_planner_method_catalog

    selected_methods = {
        method
        for value in (plan.selected_methods or {}).values()
        for method in (value if isinstance(value, list) else [value])
    }
    catalog = load_planner_method_catalog()
    executor_fields = _executor_planning_parameters_by_method()
    for method, values in method_parameters.items():
        if method not in selected_methods:
            raise ValueError(
                f"Plan Draft supplies parameters for unselected method {method!r}"
            )
        descriptor = catalog.descriptor_for_method(method) or {}
        allowed = {
            *list(descriptor.get("required_parameters") or []),
            *list((descriptor.get("parameter_defaults") or {}).keys()),
            *executor_fields.get(method, set()),
        }
        unknown = set(values).difference(allowed)
        if unknown:
            raise ValueError(
                f"Plan Draft contains unsupported parameters for {method}: "
                f"{sorted(unknown)}"
            )

    selected_stage_names = set(plan.selected_stage_names or [])
    allowed_stage_parameters: dict[str, set[str]] = {}
    for stage, values in stage_parameters.items():
        if stage not in selected_stage_names:
            raise ValueError(
                f"Plan Draft supplies parameters for unselected stage {stage!r}"
            )
        unknown = set(values).difference(allowed_stage_parameters.get(stage, set()))
        if unknown:
            raise ValueError(
                f"Plan Draft contains unsupported stage parameters for {stage}: "
                f"{sorted(unknown)}"
            )


def _executor_planning_parameters_by_method() -> dict[str, set[str]]:
    return {
        "hic_scaffolding.candidate.autohic": {"threads"},
        "t2t.alignment.minimap2": {"mode"},
    }


def _shared_parameters_from_intake(draft: Any) -> Any:
    from app.schemas.shared_parameters import SharedParameters, SpeciesFacts

    species = draft.intent.target_species
    if species is None:
        return None
    return SharedParameters(
        species_facts=SpeciesFacts(
            scientific_name=species,
            telomere_motif=draft.intent.telomere_motif,
        )
    )


def _discard_generated_plan_fields(draft: Any) -> Any:
    return draft.model_copy(
        update={
            "selected_stage_occurrences": None,
            "selected_methods": None,
            "shared_parameters": _shared_parameters_from_intake(draft),
            "method_parameters": None,
            "stage_parameters": None,
            "recommendation_policy": draft.recommendation_policy,
            "lifecycle": "planning",
        }
    )


def _copy_planner_fields_to_draft(draft: Any, plan: Any, *, lifecycle: str) -> Any:
    return draft.model_copy(
        update={
            "selected_stage_occurrences": plan.selected_stage_occurrences,
            "selected_methods": plan.selected_methods,
            "shared_parameters": plan.shared_parameters,
            "method_parameters": plan.method_parameters,
            "stage_parameters": plan.stage_parameters,
            "recommendation_policy": RecommendationPolicy.model_validate(
                (plan.entry_request or {}).get("recommendation_policy")
                or RecommendationPolicy()
            ),
            "lifecycle": lifecycle,
        }
    )


def _freeze_and_execute(draft: Any, plan: Any) -> int:
    from app.entrypoints.ask_planner import build_run_config_payload
    from app.entrypoints.run import (
        _foreground_review_available,
        execute_frozen_session,
    )
    from app.entrypoints.tui_intake import (
        materialize_manifest_draft,
        save_tui_session_draft,
    )
    from app.execution.dependency_snapshot import DependencySnapshotError
    from app.execution.run_readiness import (
        RunReadinessError,
        effective_method_closure,
        preflight_run_readiness,
    )
    from app.storage.frozen_inputs import (
        FrozenInputError,
        freeze_session_inputs,
        prepare_session_inputs,
    )
    from app.storage.paths import SessionPaths

    if not _foreground_review_available():
        print(
            "error: starting a confirmed TUI run requires a real stdin/stdout "
            "TTY before inputs can be frozen",
            file=sys.stderr,
        )
        return 1

    paths = SessionPaths(draft.data_dir, draft.session_id)
    try:
        prepared = prepare_session_inputs(
            manifest_draft=materialize_manifest_draft(draft),
            run_config_draft=build_run_config_payload(plan),
        )
        effective_methods = effective_method_closure(
            prepared.run_config,
            selected_methods=plan.selected_methods or {},
        )
        from app.reference.ncbi import species_name_from_metadata

        species_name = species_name_from_metadata(
            getattr(prepared.manifest, "user_metadata", {})
        )
        readiness_kwargs: dict[str, Any] = {"effective_methods": effective_methods}
        if species_name is not None:
            readiness_kwargs["species_name"] = species_name
        preflight_run_readiness(prepared.run_config, **readiness_kwargs)
        freeze_session_inputs(
            paths,
            prepared_inputs=prepared,
            parent_session_id=draft.parent_session_id,
            selected_methods=effective_methods,
        )
    except RunReadinessError as exc:
        print(f"error: 本次运行启动前预检失败：{exc}", file=sys.stderr)
        print(
            "本次选定的方法或 LLM 尚未就绪；请修正配置，必要时先运行 bootstrap。",
            file=sys.stderr,
        )
        return 1
    except DependencySnapshotError as exc:
        print(f"error: 无法冻结本次运行输入：{exc}", file=sys.stderr)
        print(
            "机器级运行资源尚未形成可用快照；请先运行 bootstrap 更新本机配置。",
            file=sys.stderr,
        )
        return 1
    except (FrozenInputError, OSError, ValueError) as exc:
        print(f"error: 无法冻结本次运行输入：{exc}", file=sys.stderr)
        return 1

    draft = draft.model_copy(update={"lifecycle": "frozen"})
    save_tui_session_draft(draft)
    print(f"\n已冻结本次运行输入：{paths.frozen_inputs_path}")
    draft = draft.model_copy(update={"lifecycle": "running"})
    save_tui_session_draft(draft)

    result = execute_frozen_session(
        draft.session_id,
        draft.data_dir,
        checkpointer="sqlite",
        interactive_gate=True,
    )
    lifecycle = _lifecycle_after_execution(draft, result)
    save_tui_session_draft(draft.model_copy(update={"lifecycle": lifecycle}))
    return result


def _lifecycle_after_execution(draft: Any, result_code: int) -> str:
    from app.control.lifecycle import inspect_session
    from app.storage.paths import SessionPaths

    paths = SessionPaths(draft.data_dir, draft.session_id)
    try:
        summary = inspect_session(draft.session_id, paths.run_dir)
    except (OSError, ValueError):
        return "terminal" if result_code == 0 else "frozen"
    supervision = summary.get("supervision_state")
    if supervision == "blocked_human":
        return "blocked_review"
    if supervision in {"completed", "failed_terminal"}:
        return "terminal"
    return "terminal" if result_code == 0 else "frozen"


def _resume_tui_session(data_dir: str, session_id: str) -> int:
    from app.entrypoints.review_interactive import run_interactive_review
    from app.entrypoints.tui_intake import (
        load_tui_session_draft,
        save_tui_session_draft,
    )
    from app.storage.frozen_inputs import (
        FrozenExecutionRecoveryError,
        FrozenInputError,
        frozen_execution_lock,
        frozen_execution_lock_held,
        frozen_execution_progress,
        has_frozen_input_contract,
        validate_frozen_inputs,
    )
    from app.storage.paths import SessionPaths

    paths = SessionPaths(data_dir, session_id)
    state_path = paths.tui_state_path
    if not state_path.exists():
        # Existing pre-TUI sessions may still be at a durable review gate.  This
        # is review routing only, not an AskPlan migration.
        return run_interactive_review(session_id, data_dir)

    try:
        draft = load_tui_session_draft(data_dir, session_id)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not has_frozen_input_contract(paths):
        print(f"继续草稿 {session_id}（当前阶段：{draft.lifecycle}）。\n")
        resumed_draft = _review_resumed_draft(draft)
        if resumed_draft is None:
            return 0
        return _continue_tui_draft(resumed_draft)

    try:
        validate_frozen_inputs(paths)
    except (FrozenInputError, OSError, ValueError) as exc:
        print(f"error: frozen session is incomplete or changed: {exc}", file=sys.stderr)
        return 1

    if frozen_execution_lock_held(paths):
        print(
            f"error: session {session_id} is already executing; duplicate "
            "execution was refused",
            file=sys.stderr,
        )
        return 1

    supervision = _durable_supervision_state(draft)
    if supervision == "blocked_human":
        blocked = draft.model_copy(update={"lifecycle": "blocked_review"})
        save_tui_session_draft(blocked)
        with frozen_execution_lock(paths) as lease:
            return run_interactive_review(
                session_id,
                str(paths.run_dir),
                lease=lease,
            )
    if supervision in {"completed", "failed_terminal", "terminal"}:
        return _create_child_rerun(draft)

    try:
        progress = frozen_execution_progress(paths)
    except FrozenExecutionRecoveryError as exc:
        print(f"error: frozen session cannot resume: {exc}", file=sys.stderr)
        return 1
    if progress == "partial":
        print(
            f"error: session {session_id} has partial execution evidence but "
            "no resumable checkpoint; refusing a fresh restart",
            file=sys.stderr,
        )
        frozen = draft.model_copy(update={"lifecycle": "frozen"})
        save_tui_session_draft(frozen)
        return 1

    action = "继续 checkpoint" if progress == "checkpoint" else "开始执行"
    print(f"恢复冻结运行 {session_id}：{action}。")
    running = draft.model_copy(update={"lifecycle": "running"})
    save_tui_session_draft(running)
    from app.entrypoints.run import execute_frozen_session

    result = execute_frozen_session(
        session_id,
        draft.data_dir,
        checkpointer="sqlite",
        interactive_gate=True,
    )
    lifecycle = _lifecycle_after_execution(running, result)
    save_tui_session_draft(running.model_copy(update={"lifecycle": lifecycle}))
    return result


def _review_resumed_draft(draft: Any) -> Any | None:
    """Allow bounded edits before continuing one mutable pre-freeze draft."""

    from app.entrypoints.tui_intake import save_tui_session_draft

    while True:
        _render_reused_assets(draft)
        print("恢复操作：")
        print("  Enter / 1  从下一个缺项继续")
        print("  2          修改本次目标")
        print("  3          替换 ONT reads")
        print("  4          替换 Hi-C reads / 声明没有 Hi-C")
        print("  5          替换 HiFi reads")
        print("  6          修改推荐策略")
        print("  0          返回")
        action = input("> ").strip().lower()
        if action in {"", "1", "continue", "继续"}:
            return draft
        if action in {"0", "back", "返回"}:
            return None
        if action == "2":
            draft = _replace_draft_goal(draft)
        elif action == "3":
            draft = _replace_draft_asset(draft, role="ont_reads")
        elif action == "4":
            draft = _replace_draft_asset(draft, role="hic_reads")
        elif action == "5":
            draft = _replace_draft_asset(draft, role="hifi_reads")
        elif action == "6":
            draft = _edit_recommendation_policy(draft)
        else:
            print("无法识别该选择。\n")
            continue
        save_tui_session_draft(draft)


def _edit_recommendation_policy(draft: Any) -> Any:
    from app.entrypoints.tui_intake import save_tui_session_draft
    from app.schemas.run_config import RecommendationMode

    policy = draft.recommendation_policy
    print(
        "Method/route advisor 当前模式："
        f"{policy.method_route_advisor}（advisor/deterministic_baseline）"
    )
    raw = input("> ").strip().lower() or policy.method_route_advisor
    method_route_mode = (
        cast(
            RecommendationMode,
            raw if raw in {"advisor", "deterministic_baseline"} else None,
        )
        if raw
        else policy.method_route_advisor
    )
    if method_route_mode is None:
        print("未修改：method/route advisor 模式无效。")
        method_route_mode = policy.method_route_advisor
    print(
        "Parameter advisor 当前模式："
        f"{policy.parameter_advisor}（advisor/deterministic_baseline）"
    )
    raw_parameters = input("> ").strip().lower() or policy.parameter_advisor
    parameter_mode = (
        cast(
            RecommendationMode,
            raw_parameters
            if raw_parameters in {"advisor", "deterministic_baseline"}
            else None,
        )
        if raw_parameters
        else policy.parameter_advisor
    )
    if parameter_mode is None:
        print("未修改：parameter advisor 模式无效。")
        parameter_mode = policy.parameter_advisor
    updated = draft.model_copy(
        update={
            "recommendation_policy": RecommendationPolicy(
                method_route_advisor=method_route_mode,
                parameter_advisor=parameter_mode,
            )
        }
    )
    save_tui_session_draft(updated)
    return updated


def _replace_draft_goal(draft: Any) -> Any:
    from app.entrypoints.tui_intake import (
        parse_workflow_intent,
        required_roles_for_intent,
    )
    from app.schemas.tui_intake import NoHiCFact

    print("输入新的运行目标；直接回车保留原目标：")
    request_text = input("> ").strip()
    if not request_text:
        return draft
    intent = parse_workflow_intent(
        request_text,
        llm_parser=_parse_goal_with_configured_llm,
    )
    intake = draft.intake.model_copy(
        deep=True,
        update={"requirements": required_roles_for_intent(intent)},
    )
    if "hic_reads" not in intake.requirements and isinstance(
        intake.hic_reads, NoHiCFact
    ):
        intake.hic_reads = None
    updated = draft.model_copy(
        update={
            "request_text": request_text,
            "intent": intent,
            "intake": intake,
            "degradation_decision": None,
        }
    )
    print("已更新目标；原有样本路径继续保留，规划选择已清空。\n")
    return _reset_draft_planning(updated)


def _replace_draft_asset(
    draft: Any,
    *,
    role: Literal["hifi_reads", "ont_reads", "hic_reads"],
) -> Any:
    from app.entrypoints.tui_intake import apply_asset_answer

    if role == "hifi_reads":
        print("请提供新的 PacBio HiFi 文件路径；可一次提供多个文件：")
    elif role == "ont_reads":
        print("请提供新的 ONT 文件路径；可一次提供多个文件：")
    else:
        print("请明确提供新的 Hi-C R1/R2；也可以回答\u201c没有 Hi-C\u201d：")
    answer = input("> ").strip()
    if not answer:
        print("未修改。\n")
        return draft
    result = apply_asset_answer(
        draft,
        answer=answer,
        focused_role=role,
        llm_parser=_parse_assets_with_configured_llm,
        replace_confirmed=True,
    )
    _render_asset_result(result)
    if not result.accepted_roles:
        return result.draft
    return _reset_draft_planning(result.draft)


def _reset_draft_planning(draft: Any) -> Any:
    from app.entrypoints.tui_intake import next_intake_clarification

    lifecycle = "intake" if next_intake_clarification(draft) is not None else "planning"
    return draft.model_copy(
        update={
            "selected_stage_occurrences": None,
            "selected_methods": None,
            "shared_parameters": _shared_parameters_from_intake(draft),
            "method_parameters": None,
            "stage_parameters": None,
            "recommendation_policy": draft.recommendation_policy,
            "lifecycle": lifecycle,
        }
    )


def _durable_supervision_state(draft: Any) -> str:
    from app.control.lifecycle import inspect_session
    from app.storage.paths import SessionPaths

    paths = SessionPaths(draft.data_dir, draft.session_id)
    try:
        summary = inspect_session(draft.session_id, paths.run_dir)
    except (OSError, ValueError):
        return "terminal" if draft.lifecycle == "terminal" else draft.lifecycle
    if summary.get("error"):
        return "terminal" if draft.lifecycle == "terminal" else draft.lifecycle
    return str(summary.get("supervision_state") or draft.lifecycle)


def _create_child_rerun(parent: Any) -> int:
    from app.entrypoints.run import generate_session_id
    from app.entrypoints.tui_intake import (
        create_rerun_tui_session_draft,
        parse_workflow_intent,
        save_tui_session_draft,
    )

    if not _confirm(
        "该运行已经结束。是否基于其样本资产创建一个新的运行草稿？",
        default=False,
    ):
        return 0
    print("输入新的运行目标；直接回车将保留原目标：")
    new_goal = input("> ").strip() or parent.request_text
    intent = parse_workflow_intent(
        new_goal,
        llm_parser=_parse_goal_with_configured_llm,
    )
    child = create_rerun_tui_session_draft(
        parent,
        session_id=generate_session_id(parent.data_dir),
        request_text=new_goal,
        intent=intent,
    )
    _render_reused_assets(child)
    if _has_reused_assets(child) and not _confirm(
        "以上路径已重新通过本地校验，是否在新草稿中继续使用？", default=True
    ):
        intake = child.intake.model_copy(
            update={
                "hifi_reads": None,
                "ont_reads": None,
                "hic_reads": None,
                "unresolved_candidates": [],
            }
        )
        child = child.model_copy(update={"intake": intake})
    state_path, _ = save_tui_session_draft(child)
    print(
        f"已创建子草稿 {child.session_id}，parent_session_id="
        f"{parent.session_id}\n草稿状态：{state_path}\n"
    )
    return _continue_tui_draft(child)


def _render_batch_t2t_result(result: Any) -> None:
    labels = {
        "hifi_reads": "PacBio HiFi reads",
        "ont_reads": "ONT reads",
        "hic_reads": "Hi-C paired reads",
        "no_hic": "无 Hi-C",
        "species": "物种",
        "telomere_motif": "telomere motif",
        "assembly_only_switch": "已切换为 assembly-only 路线",
    }
    for role in result.accepted_roles:
        print(f"已确认：{labels.get(role, role)}")
    for issue in result.issues:
        print(f"未接受：{issue}")
    if not result.accepted_roles and not result.issues:
        print("没有识别出可确认的信息。")
    print()


def _render_asset_result(result: Any) -> None:
    labels = {
        "hifi_reads": "PacBio HiFi reads",
        "ont_reads": "ONT reads",
        "hic_reads": "Hi-C paired reads",
        "no_hic": "无 Hi-C",
    }
    for role in result.accepted_roles:
        print(f"已确认：{labels.get(role, role)}")
    for issue in result.issues:
        print(f"未接受：{issue}")
    if not result.accepted_roles and not result.issues:
        print("没有识别出可确认的样本路径。")
    print()


def _would_replace_confirmed_asset(result: Any) -> bool:
    return any(
        "replace confirmed" in item.reason for item in result.unresolved_candidates
    )


def _render_reused_assets(draft: Any) -> None:
    if draft.intake.hifi_reads is not None:
        print("\n可复用 PacBio HiFi reads：")
        for path in draft.intake.hifi_reads.paths:
            print(f"  - {path}")
    if draft.intake.ont_reads is not None:
        print("\n可复用 ONT reads：")
        for path in draft.intake.ont_reads.paths:
            print(f"  - {path}")
    hic = draft.intake.hic_reads
    if getattr(hic, "kind", None) == "hic_paired_reads":
        print("可复用 Hi-C pairs：")
        for pair in hic.pairs:
            print(f"  - R1: {pair.r1}")
            print(f"    R2: {pair.r2}")
    if draft.intake.unresolved_candidates:
        print("下列旧路径未通过重新校验，将再次询问对应资产：")
        for item in draft.intake.unresolved_candidates:
            for path in item.candidate.paths:
                print(f"  - {path}")


def _has_reused_assets(draft: Any) -> bool:
    return (
        draft.intake.hifi_reads is not None
        or draft.intake.ont_reads is not None
        or draft.intake.hic_reads is not None
    )


def _confirm(message: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{message} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "是", "确认", "继续"}


def _is_affirmative(value: str) -> bool:
    return value.strip().lower() in {"y", "yes", "是", "确认", "继续"}


def _parse_goal_with_configured_llm(request_text: str) -> Mapping[str, Any]:
    system = (
        "Extract only explicit workflow-goal facts. Return one JSON object with "
        "goal_text, optional sample_id/target_species, target_assembly_level "
        "(contig/scaffold/chromosome), requested_stages, and "
        "Do not invent paths, "
        "parameters, methods, or machine capabilities."
    )
    return _invoke_json_model(system, request_text)


def _parse_assets_with_configured_llm(
    answer: str, focused_role: str | None
) -> Mapping[str, Any]:
    system = (
        "Extract explicitly stated sequencing asset roles from the answer. Return "
        'JSON as {"candidates": [{"role": hifi_reads|ont_reads|hic_r1|hic_r2|no_hic, '
        '"paths": [...], "explicit_role": true|false}]}. Preserve every path '
        "verbatim. Never invent a path, infer a mate from a filename, or mark a "
        "file as existing. The current focused role is "
        f"{focused_role or 'none'}."
    )
    return _invoke_json_model(system, answer)


def _invoke_json_model(system_prompt: str, user_text: str) -> dict[str, Any]:
    from app.llm.factory import build_chat_model
    from app.llm.json_response import parse_json_object, response_text

    model = build_chat_model()
    response = model.invoke([("system", system_prompt), ("human", user_text)])
    return parse_json_object(response_text(response))


def _debug_json(value: Mapping[str, Any]) -> str:
    """Stable JSON renderer kept small for interactive diagnostics and tests."""

    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
