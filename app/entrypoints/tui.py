"""Conversational sample intake, planning, and execution for Gene Agent.

The TUI owns mutable per-run facts.  It does not inspect or edit machine
configuration: the only machine-state operation is a read-only dependency
snapshot created as part of the final immutable freeze transaction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping


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
            "\u201c以卵鳎为目标物种进行染色体级组装\u201d）。"
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
        return f"draft/{draft.lifecycle}" if draft is not None else "legacy"
    try:
        validate_frozen_inputs(paths)
    except (FrozenInputError, OSError, ValueError):
        return "invalid frozen inputs"
    if draft is None:
        return "frozen legacy"
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

        result = apply_asset_answer(
            draft,
            answer=answer,
            focused_role=clarification.role,
            llm_parser=_parse_assets_with_configured_llm,
        )
        if _would_replace_confirmed_asset(result) and _confirm(
            "回答中包含已确认资产的新值，是否替换旧值？", default=False
        ):
            result = apply_asset_answer(
                draft,
                answer=answer,
                focused_role=clarification.role,
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
        _UserAbort,
    )
    from app.entrypoints.ask_planner import (
        _auto_fill_inferred_defaults,
        build_initial_plan_draft,
        merge_clarification_response,
        next_clarification,
    )
    from app.entrypoints.tui_intake import save_tui_session_draft
    from app.schemas.ask_plan import AskClarificationResponse
    from app.storage.paths import SessionPaths

    plan = _planner_state_from_tui_draft(draft)
    if plan.selected_stages is None:
        inferred = build_initial_plan_draft(plan)
        if inferred is not None:
            plan = inferred
            draft = _copy_planner_fields_to_draft(draft, plan, lifecycle="planning")
            save_tui_session_draft(draft)

    ref = SessionPaths(draft.data_dir, draft.session_id).tui_state_path
    try:
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
                    print("\n将重新选择本次运行的阶段和方法。\n")
                    plan = plan.model_copy(
                        update={
                            "selected_stages": None,
                            "resolved_execution_order": None,
                            "selected_methods": None,
                            "shared_parameters": None,
                            "method_parameters": None,
                            "stage_parameters": None,
                            "confirmed": False,
                        }
                    )
                    draft = _copy_planner_fields_to_draft(
                        draft, plan, lifecycle="planning"
                    )
                    save_tui_session_draft(draft)
                    continue
                response = AskClarificationResponse(kind="confirm", confirmed=True)
            else:
                response = _prompt_for_answer(clarification, plan)

            plan = merge_clarification_response(plan, response)
            draft = _copy_planner_fields_to_draft(draft, plan, lifecycle="planning")
            save_tui_session_draft(draft)
    except (_UserAbort, KeyboardInterrupt, EOFError):
        print("\nAborted during run planning.")
        return None


def _planner_state_from_tui_draft(draft: Any) -> Any:
    from app.schemas.ask_plan import AskPlanState
    from app.storage.paths import SessionPaths

    has_hifi = draft.intake.hifi_reads is not None
    has_ont = draft.intake.ont_reads is not None
    platform = "pacbio_hifi" if has_hifi and not has_ont else "ont"
    if has_hifi and has_ont:
        platform = None

    requested_method = draft.intent.requested_assembly_method
    selected_methods = draft.selected_methods
    method_parameters = (
        {
            method: dict(parameters)
            for method, parameters in (draft.method_parameters or {}).items()
        }
        if draft.method_parameters is not None
        else {}
    )
    if selected_methods is None and requested_method is not None:
        selected_methods = {
            "assembly": f"assembly.candidate.{requested_method}"
        }
    if requested_method == "hifiasm":
        method_id = "assembly.candidate.hifiasm"
        hifiasm_parameters = dict(method_parameters.get(method_id) or {})
        hifiasm_parameters.setdefault(
            "use_ultralong_ont",
            draft.intent.use_ultralong_ont,
        )
        hifiasm_parameters.setdefault(
            "use_hic_phasing",
            draft.intent.use_hic_phasing,
        )
        method_parameters[method_id] = hifiasm_parameters

    entry_request: dict[str, Any] = {
        "command": "run",
        "session_id": draft.session_id,
        "data_dir": draft.data_dir,
        "requested_stages": _effective_requested_stages(draft),
        "platform": platform,
        "target_species": draft.intent.target_species,
        "source_paths": [],
        "intent_parse_source": "tui_intake",
    }
    return AskPlanState(
        session_id=draft.session_id,
        data_dir=draft.data_dir,
        request_text=draft.request_text,
        entry_request=entry_request,
        manifest_ref=str(
            SessionPaths(draft.data_dir, draft.session_id).manifest_draft_path
        ),
        selected_stages=draft.selected_stages,
        resolved_execution_order=draft.resolved_execution_order,
        selected_methods=selected_methods,
        shared_parameters=draft.shared_parameters,
        method_parameters=method_parameters or None,
        stage_parameters=draft.stage_parameters,
        confirmed=False,
    )


def _effective_requested_stages(draft: Any) -> list[str]:
    from app.schemas.tui_intake import NoHiCFact
    from app.workflow import stages_through

    stages = list(draft.intent.requested_stages)
    degraded = (
        isinstance(draft.intake.hic_reads, NoHiCFact)
        and draft.degradation_decision is not None
        and draft.degradation_decision.confirmed
    )
    if degraded:
        stages = stages_through("assembly_review")
    return stages


def _copy_planner_fields_to_draft(draft: Any, plan: Any, *, lifecycle: str) -> Any:
    return draft.model_copy(
        update={
            "selected_stages": plan.selected_stages,
            "resolved_execution_order": plan.resolved_execution_order,
            "selected_methods": plan.selected_methods,
            "shared_parameters": plan.shared_parameters,
            "method_parameters": plan.method_parameters,
            "stage_parameters": plan.stage_parameters,
            "lifecycle": lifecycle,
        }
    )


def _freeze_and_execute(draft: Any, plan: Any) -> int:
    from app.entrypoints.ask_planner import build_run_config_payload
    from app.entrypoints.run import execute_frozen_session
    from app.entrypoints.tui_intake import (
        materialize_manifest_draft,
        save_tui_session_draft,
    )
    from app.execution.dependency_snapshot import DependencySnapshotError
    from app.storage.frozen_inputs import FrozenInputError, freeze_session_inputs
    from app.storage.paths import SessionPaths

    paths = SessionPaths(draft.data_dir, draft.session_id)
    try:
        freeze_session_inputs(
            paths,
            manifest_draft=materialize_manifest_draft(draft),
            run_config_draft=build_run_config_payload(plan),
            parent_session_id=draft.parent_session_id,
            selected_stages=plan.resolved_execution_order or [],
            selected_methods=plan.selected_methods or {},
        )
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
        draft = _review_resumed_draft(draft)
        if draft is None:
            return 0
        return _continue_tui_draft(draft)

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
        with frozen_execution_lock(paths):
            return run_interactive_review(session_id, str(paths.run_dir))
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
        else:
            print("无法识别该选择。\n")
            continue
        save_tui_session_draft(draft)


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


def _replace_draft_asset(draft: Any, *, role: str) -> Any:
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
            "selected_stages": None,
            "resolved_execution_order": None,
            "selected_methods": None,
            "shared_parameters": None,
            "method_parameters": None,
            "stage_parameters": None,
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
        "(contig/scaffold/chromosome), requested_stages, optional explicit "
        "requested_assembly_method (flye/nextdenovo/hifiasm/hifiasm_ont), and "
        "explicit use_ultralong_ont/use_hic_phasing booleans. Do not invent paths, "
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
