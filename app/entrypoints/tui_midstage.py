"""Explicit material intake for entering at Assembly or Assembly Review."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.schemas.tui_intake import (
    AssetAnswerCandidate,
    CleanLongReadsFact,
    EntryMode,
    ExistingAssemblyFact,
    IntakeAnswerRecord,
    LongReadPlatform,
    TuiSessionDraft,
    UnresolvedAssetCandidate,
)

if TYPE_CHECKING:
    from app.entrypoints.tui_intake import AssetAnswerApplyResult


def explicit_entry_mode(text: str) -> EntryMode:
    if re.search(
        r"已有(?:的)?组装|现有组装|组装好的|existing[_ -]assembly|assembled genome",
        text,
        re.IGNORECASE,
    ):
        return "existing_assembly"
    if re.search(
        r"clean[_ -](?:long[_ -])?reads|clean\s*(?:hifi|ont|长读段)|"
        r"(?:清洗后|已清洗|质控后|已质控)\s*(?:ont|hifi|pacbio\s*hifi)\b|"
        r"(?:清洗后|已清洗|质控后|已质控)(?:的)?(?:长读段|读段|数据)|"
        r"(?:hifi|ont).{0,12}(?:已清洗|已质控)",
        text,
        re.IGNORECASE,
    ):
        return "clean_long_reads"
    return "raw_reads"


def explicit_long_read_platform(text: str) -> LongReadPlatform | None:
    ont = bool(re.search(r"\bont\b|\bnanopore\b|纳米孔", text, re.IGNORECASE))
    hifi = bool(re.search(r"\bhifi\b|\bccs\b|高保真", text, re.IGNORECASE))
    if ont == hifi:
        return None
    return "ont" if ont else "pacbio_hifi"


def apply_midstage_answer(
    draft: TuiSessionDraft, *, answer: str, replace_confirmed: bool
) -> AssetAnswerApplyResult:
    from app.entrypoints.tui_intake import (
        AssetAnswerApplyResult,
        _mask_path_spans,
        extract_path_spans,
        validate_candidate_paths,
    )

    mode = draft.intent.entry_mode
    assert mode != "raw_reads"
    spans = extract_path_spans(answer)
    paths = list(dict.fromkeys(span.path for span in spans))
    issues: list[str] = []
    accepted: list[str] = []
    working = draft.model_copy(deep=True)
    intake = working.intake
    candidate = (
        AssetAnswerCandidate(role=mode, paths=paths, explicit_role=True)
        if paths
        else None
    )
    material_text = _mask_path_spans(answer, spans)
    platform = explicit_long_read_platform(material_text)
    if mode == "clean_long_reads":
        ont = bool(
            re.search(r"\bont\b|\bnanopore\b|纳米孔", material_text, re.IGNORECASE)
        )
        hifi = bool(re.search(r"\bhifi\b|\bccs\b|高保真", material_text, re.IGNORECASE))
        if ont and hifi:
            issues.append("当前 clean 读段入口需一个明确平台；请分别声明材料。")
        platform = platform or draft.intent.clean_reads_platform
        if platform is None:
            issues.append(
                "请明确 clean 长读段平台为 ONT 或 PacBio HiFi；不从文件名推断。"
            )
    if candidate is None:
        issues.append("请提供明确的文件路径。")
    else:
        validation = validate_candidate_paths(candidate)
        issues.extend(validation.issues)
        if mode == "existing_assembly" and len(paths) != 1:
            issues.append("已有组装入口必须恰好提供一个 FASTA。")
        if not issues:
            fact: CleanLongReadsFact | ExistingAssemblyFact
            if mode == "clean_long_reads":
                assert platform is not None
                fact = CleanLongReadsFact(paths=validation.paths, platform=platform)
            else:
                fact = ExistingAssemblyFact(paths=validation.paths)
            existing = getattr(intake, mode)
            if existing is not None and existing != fact and not replace_confirmed:
                issues.append(
                    "would replace confirmed input; explicitly confirm replacement"
                )
            else:
                setattr(intake, mode, fact)
                if mode == "clean_long_reads":
                    working.intent.clean_reads_platform = platform
                accepted.append(mode)
                intake.unresolved_candidates = [
                    item
                    for item in intake.unresolved_candidates
                    if item.candidate.role != mode
                ]
        if issues:
            intake.unresolved_candidates.append(
                UnresolvedAssetCandidate(candidate=candidate, reason="; ".join(issues))
            )
    intake.answer_history.append(
        IntakeAnswerRecord(
            raw_answer=answer,
            focused_role=mode,
            candidates=[candidate] if candidate is not None else [],
            accepted_roles=accepted,
            issues=issues,
        )
    )
    return AssetAnswerApplyResult(
        draft=working,
        accepted_roles=accepted,
        issues=issues,
        unresolved_candidates=intake.unresolved_candidates,
        llm_used=False,
    )


def reuse_midstage_fact(parent: TuiSessionDraft, child: TuiSessionDraft) -> None:
    """Revalidate only the material matching the child's explicit entry mode."""
    from app.entrypoints.tui_intake import validate_candidate_paths

    mode = child.intent.entry_mode
    if mode == "raw_reads" or mode != parent.intent.entry_mode:
        return
    fact = getattr(parent.intake, mode)
    if fact is None:
        return
    candidate = AssetAnswerCandidate(role=mode, paths=fact.paths, explicit_role=True)
    validation = validate_candidate_paths(candidate)
    if validation.valid:
        setattr(child.intake, mode, fact.model_copy(update={"paths": validation.paths}))
        if isinstance(fact, CleanLongReadsFact):
            child.intent.clean_reads_platform = fact.platform
    else:
        child.intake.unresolved_candidates.append(
            UnresolvedAssetCandidate(
                candidate=candidate, reason="; ".join(validation.issues)
            )
        )
