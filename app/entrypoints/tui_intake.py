"""Deterministic, durable sample-intake primitives for ``gene-agent tui``.

This module collects only sample facts and never looks up machine
configuration, scans a directory, or infers a sequencing role from a file name.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import ValidationError

from app.schemas.manifest import InputAssetSpec, InputManifest, PairedReadSpec
from app.schemas.shared_parameters import (
    SharedParameters,
    SpeciesFacts,
    normalize_dna_motif,
)
from app.schemas.tui_intake import (
    AssetAnswerCandidate,
    AssetCollectionState,
    AssetPathSpan,
    DegradationDecision,
    HiCPairedReadsFact,
    HiCReadPair,
    HifiReadsFact,
    IntakeAnswerRecord,
    IntakeRole,
    NoHiCFact,
    OntReadsFact,
    TuiSessionDraft,
    UnresolvedAssetCandidate,
    WorkflowIntent,
)
from app.storage.paths import SessionPaths, ensure_run_layout

_FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")
_FASTA_SUFFIXES = (".fasta.gz", ".fa.gz", ".fna.gz", ".fasta", ".fa", ".fna")
_ONT_SUFFIXES = _FASTQ_SUFFIXES + _FASTA_SUFFIXES

# A path must have an explicit path prefix.  The lexer intentionally retains
# unsupported extensions and directories so local validation can report them
# to the operator; it never inspects a directory or guesses a path.
_PATH_TOKEN_RE = re.compile(
    r"(?P<path>(?:~|/|\.\./|\./|[A-Za-z0-9][A-Za-z0-9._-]*/)"
    r"[^\s'\"，,；;：:。()\[\]{}<>]+)",
    flags=re.IGNORECASE,
)
_CLAUSE_BOUNDARY_RE = re.compile(r"[;；\n\r。]")
_ONT_LABEL_RE = re.compile(r"(?:\bont\b|\bnanopore\b|纳米孔)", re.IGNORECASE)
_HIFI_LABEL_RE = re.compile(
    r"(?:\bhifi\b|\bpacbio\b|\bccs\b|高保真(?:长)?读段)",
    re.IGNORECASE,
)
_HIC_LABEL_RE = re.compile(
    r"(?:\bhi[\s-]?c\b|高通量染色体构象捕获)",
    re.IGNORECASE,
)
# Explicit Hi-C phasing: hifiasm+Hi-C, phase/haplotype wording, or --h1/--h2.
_HIC_PHASING_RE = re.compile(
    r"(?:"
    r"--h[12]\b"
    r"|hi[\s-]?c\s+(?:phase|phasing|haplotype|校正|分相|定相)"
    r"|(?:phase|phasing|haplotype|校正|分相|定相)\s{0,8}hi[\s-]?c"
    r"|hifiasm.{0,60}hi[\s-]?c"
    r"|hi[\s-]?c.{0,60}hifiasm"
    r")",
    re.IGNORECASE,
)
# Hi-C mentioned only in a scaffolding context (not phasing).
_HIC_SCAFFOLDING_ONLY_RE = re.compile(
    r"(?:scaffold|挂载|挂靠|染色体级别|chromosome[-\s]level).{0,60}hi[\s-]?c"
    r"|hi[\s-]?c.{0,60}(?:scaffold|挂载|挂靠|染色体级别|chromosome[-\s]level)",
    re.IGNORECASE,
)
_R1_LABEL_RE = re.compile(r"(?:\br[ _-]?1\b|\bread[ _-]?1\b|读段\s*1)", re.IGNORECASE)
_R2_LABEL_RE = re.compile(r"(?:\br[ _-]?2\b|\bread[ _-]?2\b|读段\s*2)", re.IGNORECASE)
_NO_HIC_RE = re.compile(
    r"(?:没有|无|不提供|没有任何)\s*(?:hi[\s-]?c|高通量染色体构象捕获)"
    r"|(?:\bno\b|\bwithout\b)\s*(?:hi[\s-]?c)",
    re.IGNORECASE,
)
_TARGET_SPECIES_PATTERNS = (
    re.compile(r"以(?P<species>[^，,。；;\s]{1,80}?)为目标物种"),
    re.compile(r"目标物种(?:是|为|[:：])(?P<species>[^，,。；;\s]{1,80})"),
)

# Batch T2T answer patterns (species / motif / scope switch).
# ASCII quotes only; smart/curly quotes not expected in user-typed input.
_BATCH_SPECIES_RE = re.compile(
    r"(?:物种|species)\s*[:\uff1a]\s*[\"']?"
    r"(?P<species>[^\"'\uff0c,。\uff1b;\n\r]{1,80})"
    r"[\"']?",
    re.IGNORECASE,
)
_BATCH_MOTIF_RE = re.compile(
    r"motif[^:\uff1a\n]{0,30}[:\uff1a]\s*[\"']?"
    r"(?P<motif>[ACGT]{2,40})(?![a-zA-Z0-9])"
    r"[\"']?",
    re.IGNORECASE,
)
_UNKNOWN_MOTIF_RE = re.compile(
    r"motif[^:：\n]*[:：][^\n]*(?:unknown|不知道|未知|none|null|\?)",
    re.IGNORECASE,
)
_ASSEMBLY_ONLY_RE = re.compile(
    r"改为只做\s*assembly|只做\s*assembly|仅做\s*assembly"
    r"|不开\s*[Tt]2[Tt]|assembly[\s-]only|skip[\s-]t2t",
    re.IGNORECASE,
)

_PENDING_HIC_MATE_REASON = "awaiting one explicit complementary Hi-C mate"


@dataclass(frozen=True)
class BatchT2TParseResult:
    """Biological facts parsed from one batch T2T response."""

    species: str | None
    motif_raw: str | None        # validated DNA motif string, or None
    motif_explicit_unknown: bool  # True if user said unknown/不知道/none
    assembly_only_switch: bool
    issues: list[str]


class TuiDraftConflictError(ValueError):
    """Raised when another writer incremented the draft revision."""


@dataclass(frozen=True)
class IntakeClarification:
    """The next deterministic prompt the top-level TUI should render."""

    kind: Literal["asset_collection", "degradation_confirmation", "t2t_batch"]
    role: Literal[
        "hifi_reads",
        "ont_reads",
        "hic_reads",
        "no_hic_degradation",
        "t2t_facts",
    ]
    question: str


@dataclass(frozen=True)
class ParsedAssetAnswer:
    """Locally parsed candidates from one raw answer."""

    path_spans: list[AssetPathSpan]
    candidates: list[AssetAnswerCandidate]
    unresolved_paths: list[str]
    issues: list[str]
    llm_used: bool = False


@dataclass(frozen=True)
class PathValidationResult:
    """Outcome of bounded local validation for one candidate role."""

    paths: list[str]
    issues: list[str]

    @property
    def valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class AssetAnswerApplyResult:
    """Updated draft plus the information the TUI should render."""

    draft: TuiSessionDraft
    accepted_roles: list[str]
    issues: list[str]
    unresolved_candidates: list[UnresolvedAssetCandidate]
    llm_used: bool


def deterministic_workflow_intent(request_text: str) -> WorkflowIntent:
    """Return conservative intake hints when the goal parser is unavailable.

    The recognizer intentionally has a small vocabulary. Unknown goals still
    produce a usable intake draft. These hints select follow-up questions only;
    they are not an execution plan or a fallback Plan Draft.
    """

    from app.workflow import stages_through

    text = request_text.strip()
    if not text:
        raise ValueError("workflow goal must not be empty")
    lower = text.lower()
    requested_stages: list[str]
    target_assembly_level: Literal["contig", "scaffold", "chromosome"]
    if any(token in lower for token in ("t2t", "telomere-to-telomere", "端粒到端粒")):
        requested_stages = stages_through("t2t")
        target_assembly_level = "chromosome"
    elif any(token in lower for token in ("hi-c", "hic", "染色体级", "chromosome")):
        requested_stages = stages_through("hic_scaffolding")
        target_assembly_level = "chromosome"
    elif any(token in lower for token in ("scaffold", "挂载")):
        requested_stages = stages_through("assembly")
        target_assembly_level = "scaffold"
    elif any(token in lower for token in ("assembly", "assemble", "组装")):
        requested_stages = stages_through("assembly")
        target_assembly_level = "contig"
    else:
        requested_stages = []
        target_assembly_level = "contig"

    requested_assembly_method: Literal[
        "flye",
        "nextdenovo",
        "hifiasm",
        "hifiasm_ont",
    ] | None = None
    if "hifiasm" in lower:
        ont_mode = bool(
            re.search(
                r"hifiasm(?:[_ -]?ont)|(?:--ont\b)|hifiasm.{0,12}(?:ont模式|ont mode)",
                lower,
            )
        )
        requested_assembly_method = "hifiasm_ont" if ont_mode else "hifiasm"
    elif "nextdenovo" in lower:
        requested_assembly_method = "nextdenovo"
    elif re.search(r"\bflye\b", lower):
        requested_assembly_method = "flye"
    elif re.search(r"(?:\bhifi\b|\bccs\b|pacbio[ _-]?hi[- ]?fi)", lower):
        requested_assembly_method = "hifiasm"

    use_ultralong_ont = requested_assembly_method == "hifiasm" and bool(
        re.search(
            r"(?:--ul\b|ultra[- ]?long.{0,12}(?:ont|nanopore)|"
            r"(?:ont|nanopore).{0,12}ultra[- ]?long|超长.{0,8}(?:ont|纳米孔)|"
            r"(?:ont|纳米孔).{0,8}超长)",
            lower,
        )
    )
    use_hic_phasing = requested_assembly_method == "hifiasm" and (
        bool(_HIC_PHASING_RE.search(text))
        and not (
            bool(_HIC_SCAFFOLDING_ONLY_RE.search(text))
            and not bool(re.search(r"--h[12]\b", lower))
        )
    )

    target_species = _extract_target_species(text)
    return WorkflowIntent(
        goal_text=text,
        target_species=target_species,
        target_assembly_level=target_assembly_level,
        requested_stages=requested_stages,
        requested_assembly_method=requested_assembly_method,
        use_ultralong_ont=use_ultralong_ont,
        use_hic_phasing=use_hic_phasing,
    )


def parse_workflow_intent(
    request_text: str,
    *,
    llm_parser: Callable[[str], WorkflowIntent | Mapping[str, Any] | str] | None = None,
) -> WorkflowIntent:
    """Parse intake-routing facts with an optional LLM and a local fallback.

    A failing model, malformed JSON, or invalid model schema never prevents the
    TUI from asking its fixed sample questions. The result is not planning
    authority: the editable Plan Draft is generated separately after intake.
    """

    fallback = deterministic_workflow_intent(request_text)
    if llm_parser is None:
        return fallback
    try:
        parsed = _coerce_workflow_intent(llm_parser(request_text))
    except Exception:
        return fallback
    # Keep the raw request authoritative and use deterministic recognition only
    # to fill missing intake hints. It must never overwrite a model-supplied
    # stage hint or become the later Plan Draft.
    target_assembly_level = (
        parsed.target_assembly_level
        if "target_assembly_level" in parsed.model_fields_set
        else fallback.target_assembly_level
    )
    return WorkflowIntent(
        goal_text=fallback.goal_text,
        sample_id=parsed.sample_id if parsed.sample_id else fallback.sample_id,
        target_species=parsed.target_species or fallback.target_species,
        target_assembly_level=target_assembly_level,
        requested_stages=parsed.requested_stages or fallback.requested_stages,
        requested_assembly_method=(
            parsed.requested_assembly_method or fallback.requested_assembly_method
        ),
        use_ultralong_ont=(
            fallback.use_ultralong_ont or parsed.use_ultralong_ont
        ),
        use_hic_phasing=(
            fallback.use_hic_phasing or parsed.use_hic_phasing
        ),
    )


def required_roles_for_intent(intent: WorkflowIntent) -> list[IntakeRole]:
    """Compute sample facts required by this scope's assembly-oriented goals."""

    stages = set(intent.requested_stages)
    requires_hic = "hic_scaffolding" in stages or (
        intent.target_assembly_level == "chromosome"
    )
    method = intent.requested_assembly_method
    roles: list[IntakeRole] = (
        ["hifi_reads"] if method == "hifiasm" else ["ont_reads"]
    )
    if method == "hifiasm" and intent.use_ultralong_ont:
        roles.append("ont_reads")
    if method == "hifiasm_ont":
        roles = ["ont_reads"]
    if requires_hic or intent.use_hic_phasing:
        roles.append("hic_reads")
    return list(dict.fromkeys(roles))


def create_tui_session_draft(
    *,
    session_id: str,
    data_dir: str,
    request_text: str,
    intent: WorkflowIntent | None = None,
    parent_session_id: str | None = None,
) -> TuiSessionDraft:
    """Create an editable v2 TUI draft before any manifest is materialized."""

    canonical_data_dir = str(Path(data_dir).expanduser().resolve())
    resolved_intent = intent or deterministic_workflow_intent(request_text)
    return TuiSessionDraft(
        session_id=session_id,
        data_dir=canonical_data_dir,
        parent_session_id=parent_session_id,
        request_text=request_text,
        intent=resolved_intent,
        intake=AssetCollectionState(
            requirements=required_roles_for_intent(resolved_intent)
        ),
    )


def next_intake_clarification(draft: TuiSessionDraft) -> IntakeClarification | None:
    """Return the one next missing intake item, derived solely from draft facts."""

    requirements = set(draft.intake.requirements)
    stages = set(draft.intent.requested_stages)

    if "hifi_reads" in requirements and draft.intake.hifi_reads is None:
        return IntakeClarification(
            kind="asset_collection",
            role="hifi_reads",
            question=(
                "需要本次样本的 PacBio HiFi reads。请直接提供文件路径；"
                "可一次提供多个文件。"
            ),
        )

    if "ont_reads" in requirements and draft.intake.ont_reads is None:
        return IntakeClarification(
            kind="asset_collection",
            role="ont_reads",
            question=(
                "需要本次样本的 ONT reads。请直接提供文件路径；可一次提供多个文件。"
            ),
        )

    # A confirmed no-Hi-C degradation effectively removes hic_scaffolding and t2t
    # from the required scope without modifying the durable intent stages.
    degraded = (
        isinstance(draft.intake.hic_reads, NoHiCFact)
        and draft.degradation_decision is not None
        and draft.degradation_decision.confirmed
    )
    effective_stages = stages - {"hic_scaffolding", "t2t"} if degraded else stages

    needs_hic = "hic_reads" in requirements
    needs_species = "t2t" in effective_stages
    show_motif = "t2t" in effective_stages and not draft.intent.motif_choice_complete

    # Check for NoHiCFact degradation confirmation before asking for other T2T facts.
    if needs_hic:
        if isinstance(draft.intake.hic_reads, NoHiCFact) and not (
            draft.degradation_decision and draft.degradation_decision.confirmed
        ):
            return IntakeClarification(
                kind="degradation_confirmation",
                role="no_hic_degradation",
                question=(
                    "未提供 Hi-C 将降级为 contig 路线，不能完成 chromosome-scale "
                    "scaffolding 及后续 T2T 主链。确认继续吗？"
                ),
            )

    # After required long reads are known and degradation is confirmed, check T2T facts.
    hic_missing = needs_hic and draft.intake.hic_reads is None
    species_missing = needs_species and draft.intent.target_species is None

    if hic_missing or species_missing or show_motif:
        return IntakeClarification(
            kind="t2t_batch",
            role="t2t_facts",
            question=_build_t2t_batch_question(draft),
        )

    return None


def _build_t2t_batch_question(draft: TuiSessionDraft) -> str:
    """Compose the batched T2T fact prompt from the current missing facts."""

    stages = set(draft.intent.requested_stages)
    needs_hic = "hic_reads" in set(draft.intake.requirements)
    needs_species = "t2t" in stages
    show_motif = "t2t" in stages and not draft.intent.motif_choice_complete

    hic_missing = needs_hic and draft.intake.hic_reads is None
    species_missing = needs_species and draft.intent.target_species is None

    parts: list[str] = ["还缺少当前 Hi-C -> T2T 主链所需信息，请按类似格式一次提供：\n"]
    if hic_missing:
        parts.append(
            'Hi-C（供上游 scaffolding）：R1="/data/hic_1.fastq.gz"，'
            'R2="/data/hic_2.fastq.gz"'
        )
    if species_missing:
        parts.append('物种："人类"')
    if show_motif:
        parts.append('motif（可选）："TTAGGG"')
    parts.append('\n也可以回复"改为只做 assembly"。')
    return "\n".join(parts)


def confirm_no_hic_degradation(
    draft: TuiSessionDraft, *, confirmed: bool
) -> TuiSessionDraft:
    """Record the explicit operator decision required after ``NoHiCFact``."""

    if not isinstance(draft.intake.hic_reads, NoHiCFact):
        raise ValueError(
            "no Hi-C degradation can only be confirmed after a no_hic fact"
        )
    return draft.model_copy(
        update={"degradation_decision": DegradationDecision(confirmed=confirmed)}
    )


def parse_batch_t2t_answer(answer: str) -> BatchT2TParseResult:
    """Extract species, motif, and scope-switch signals from a batch T2T answer.

    Deterministic labelled extraction is used.  Species and motif values are
    taken verbatim (after stripping surrounding syntax from the prompt labels
    and matching quotation marks).  An invalid motif is reported as an issue
    without setting ``motif_raw``; it does not convert to deferred resolution.
    """

    issues: list[str] = []

    # Assembly-only scope switch takes priority.
    assembly_only_switch = bool(_ASSEMBLY_ONLY_RE.search(answer))

    # Species extraction: strip label and surrounding quotes.
    species: str | None = None
    species_match = _BATCH_SPECIES_RE.search(answer)
    if species_match:
        raw_species = species_match.group("species").strip().strip("\"'""''")
        if raw_species:
            species = raw_species

    # Motif extraction: validate against DNA motif rules.
    motif_raw: str | None = None
    motif_explicit_unknown = False
    motif_match = _BATCH_MOTIF_RE.search(answer)
    if motif_match:
        candidate = motif_match.group("motif").strip()
        try:
            motif_raw = normalize_dna_motif(candidate)
        except ValueError as exc:
            issues.append(f"invalid telomere motif {candidate!r}: {exc}")
    elif _UNKNOWN_MOTIF_RE.search(answer):
        motif_explicit_unknown = True

    return BatchT2TParseResult(
        species=species,
        motif_raw=motif_raw,
        motif_explicit_unknown=motif_explicit_unknown,
        assembly_only_switch=assembly_only_switch,
        issues=issues,
    )


def project_species_to_shared_parameters(draft: TuiSessionDraft) -> TuiSessionDraft:
    """Sync species/motif from intake staging into shared_parameters.species_facts.

    Constructs or updates ``SharedParameters`` only when ``target_species``
    exists.  A motif staged in ``intent.telomere_motif`` is written to
    ``species_facts.telomere_motif``; if species exists but motif is not staged
    the telomere_motif field is left as-is in existing facts (or absent in new
    facts).  Platform, read type, genome size, and other shared facts are
    preserved unchanged.
    """

    species = draft.intent.target_species
    if species is None:
        return draft

    current_shared = draft.shared_parameters or SharedParameters()
    current_facts = current_shared.species_facts
    existing: dict[str, object] = (
        current_facts.model_dump(exclude_none=True) if current_facts else {}
    )
    existing["scientific_name"] = species
    staged_motif = draft.intent.telomere_motif
    if staged_motif is not None:
        existing["telomere_motif"] = staged_motif
    new_facts = SpeciesFacts.model_validate(existing)
    new_shared = current_shared.model_copy(update={"species_facts": new_facts})
    return draft.model_copy(update={"shared_parameters": new_shared})


def apply_batch_t2t_facts(
    draft: TuiSessionDraft,
    *,
    answer: str,
    llm_parser: Callable[
        [str, IntakeRole | None],
        Sequence[AssetAnswerCandidate | Mapping[str, Any]] | Mapping[str, Any] | str,
    ]
    | None = None,
    replace_confirmed: bool = False,
) -> AssetAnswerApplyResult:
    """Apply one batch T2T answer (Hi-C paths, species, motif, scope switch).

    Processing order:
    1. Assembly-only switch (wins over all other facts for stage selection).
    2. Hi-C paths via the existing exact-path pipeline.
    3. Species label — verbatim, no alias resolution.
    4. Telomere motif — validated; invalid motif is reported but not stored.
    5. Motif-choice completion — set True on any non-empty batch answer that
       does not contain an invalid-only motif error.
    6. Species projection into SharedParameters.
    """

    from app.workflow import stages_through

    batch = parse_batch_t2t_answer(answer)
    working = draft.model_copy(deep=True)
    all_issues: list[str] = list(batch.issues)
    accepted_roles: list[str] = []

    # --- 1. Assembly-only scope switch ---
    if batch.assembly_only_switch:
        assembly_stages = stages_through("assembly_review")
        new_intent = working.intent.model_copy(
            update={
                "requested_stages": assembly_stages,
                "target_assembly_level": "contig",
            }
        )
        new_requirements = [
            r for r in working.intake.requirements if r != "hic_reads"
        ]
        new_intake = working.intake.model_copy(
            update={"requirements": new_requirements}
        )
        working = working.model_copy(
            update={
                "intent": new_intent,
                "intake": new_intake,
                "degradation_decision": None,
            }
        )
        accepted_roles.append("assembly_only_switch")

    # --- 2. Hi-C paths via existing pipeline ---
    if "hic_reads" in set(working.intake.requirements):
        path_result = apply_asset_answer(
            working,
            answer=answer,
            focused_role="hic_reads",
            llm_parser=llm_parser,
            replace_confirmed=replace_confirmed,
        )
        working = path_result.draft
        accepted_roles.extend(
            r for r in path_result.accepted_roles if r not in accepted_roles
        )
        all_issues.extend(path_result.issues)
    elif not batch.assembly_only_switch:
        # ONT may be in this answer even without hic requirement
        path_result = apply_asset_answer(
            working,
            answer=answer,
            focused_role=None,
            llm_parser=llm_parser,
            replace_confirmed=replace_confirmed,
        )
        working = path_result.draft
        accepted_roles.extend(
            r for r in path_result.accepted_roles if r not in accepted_roles
        )
        all_issues.extend(path_result.issues)

    # --- 3. Species ---
    if batch.species is not None:
        existing_species = working.intent.target_species
        if existing_species is not None and existing_species != batch.species:
            if not replace_confirmed:
                all_issues.append(
                    "species is already confirmed; explicitly confirm replacement "
                    "before changing it"
                )
            else:
                working = working.model_copy(
                    update={
                        "intent": working.intent.model_copy(
                            update={"target_species": batch.species}
                        )
                    }
                )
                accepted_roles.append("species")
        else:
            working = working.model_copy(
                update={
                    "intent": working.intent.model_copy(
                        update={"target_species": batch.species}
                    )
                }
            )
            accepted_roles.append("species")

    # --- 4 & 5. Motif + motif_choice_complete ---
    has_invalid_motif_only = bool(batch.issues) and batch.motif_raw is None
    batch_has_accepted_content = bool(
        accepted_roles or batch.species is not None
    )

    if batch.motif_raw is not None:
        existing_motif = working.intent.telomere_motif
        if existing_motif is not None and existing_motif != batch.motif_raw:
            if not replace_confirmed:
                all_issues.append(
                    "telomere motif is already confirmed; explicitly confirm "
                    "replacement before changing it"
                )
            else:
                working = working.model_copy(
                    update={
                        "intent": working.intent.model_copy(
                            update={
                                "telomere_motif": batch.motif_raw,
                                "motif_choice_complete": True,
                            }
                        )
                    }
                )
                accepted_roles.append("telomere_motif")
        else:
            working = working.model_copy(
                update={
                    "intent": working.intent.model_copy(
                        update={
                            "telomere_motif": batch.motif_raw,
                            "motif_choice_complete": True,
                        }
                    )
                }
            )
            accepted_roles.append("telomere_motif")
    elif (
        not working.intent.motif_choice_complete
        and (batch.motif_explicit_unknown or batch_has_accepted_content)
        and not has_invalid_motif_only
    ):
        # Omission or explicit unknown on a non-empty batch → deferred.
        working = working.model_copy(
            update={
                "intent": working.intent.model_copy(
                    update={"motif_choice_complete": True}
                )
            }
        )

    # --- 6. Project species/motif into SharedParameters ---
    working = project_species_to_shared_parameters(working)

    return AssetAnswerApplyResult(
        draft=working,
        accepted_roles=accepted_roles,
        issues=all_issues,
        unresolved_candidates=working.intake.unresolved_candidates,
        llm_used=False,
    )


def extract_path_spans(answer: str) -> list[AssetPathSpan]:
    """Extract exact supported-read path spans without resolving roles."""

    spans: list[AssetPathSpan] = []
    seen: set[str] = set()
    for match in _PATH_TOKEN_RE.finditer(answer):
        path = match.group("path")
        if not path:
            continue
        if path in seen:
            continue
        seen.add(path)
        start = match.start("path")
        spans.append(AssetPathSpan(path=path, start=start, end=start + len(path)))
    return spans


def parse_asset_answer_deterministically(
    answer: str,
    *,
    focused_role: IntakeRole | None,
) -> ParsedAssetAnswer:
    """Parse all explicit roles in an answer, not only the current question.

    An unlabelled path is assigned only to the focused long-read question.
    Hi-C always requires explicit R1/R2 attribution in the text (or a later
    structured confirmation); file names are masked before role analysis.
    """

    spans = extract_path_spans(answer)
    masked = _mask_path_spans(answer, spans)
    grouped: dict[str, list[AssetPathSpan]] = {
        "hifi_reads": [],
        "ont_reads": [],
        "hic_r1": [],
        "hic_r2": [],
    }
    explicit_roles: dict[str, bool] = {
        "hifi_reads": False,
        "ont_reads": False,
        "hic_r1": False,
        "hic_r2": False,
    }
    unresolved: list[str] = []

    for span in spans:
        role, explicit = _role_for_path_span(
            span=span,
            masked_answer=masked,
            focused_role=focused_role,
        )
        if role is None:
            unresolved.append(span.path)
            continue
        grouped[role].append(span)
        explicit_roles[role] = explicit_roles[role] or explicit

    candidates: list[AssetAnswerCandidate] = []
    for role in ("hifi_reads", "ont_reads", "hic_r1", "hic_r2"):
        role_spans = grouped[role]
        if not role_spans:
            continue
        candidates.append(
            AssetAnswerCandidate(
                role=role,
                paths=[span.path for span in role_spans],
                source_spans=role_spans,
                explicit_role=explicit_roles[role],
            )
        )
    if _NO_HIC_RE.search(masked):
        candidates.append(AssetAnswerCandidate(role="no_hic", explicit_role=True))

    issues = []
    if unresolved:
        issues.append(
            "unclassified paths were not accepted because no explicit role "
            "was provided: " + ", ".join(unresolved)
        )
    return ParsedAssetAnswer(
        path_spans=spans,
        candidates=candidates,
        unresolved_paths=unresolved,
        issues=issues,
    )


def parse_asset_answer_with_fallback(
    answer: str,
    *,
    focused_role: IntakeRole | None,
    llm_parser: Callable[
        [str, IntakeRole | None],
        Sequence[AssetAnswerCandidate | Mapping[str, Any]] | Mapping[str, Any] | str,
    ]
    | None = None,
) -> ParsedAssetAnswer:
    """Use optional LLM candidates only after deterministic evidence checks.

    This is deliberately usable when the LLM call or its output fails: the
    deterministic parse remains the complete fallback rather than an error
    path that stops the TUI.
    """

    parsed = parse_asset_answer_deterministically(answer, focused_role=focused_role)
    if llm_parser is None:
        return parsed
    try:
        supplied = _coerce_llm_candidates(llm_parser(answer, focused_role))
    except Exception:
        return parsed
    accepted, issues = _safe_llm_candidates(
        supplied,
        raw_answer=answer,
        deterministic=parsed,
    )
    if not accepted:
        return ParsedAssetAnswer(
            path_spans=parsed.path_spans,
            candidates=parsed.candidates,
            unresolved_paths=parsed.unresolved_paths,
            issues=[*parsed.issues, *issues],
            llm_used=False,
        )
    merged = _merge_candidates(parsed.candidates, accepted)
    return ParsedAssetAnswer(
        path_spans=parsed.path_spans,
        candidates=merged,
        unresolved_paths=parsed.unresolved_paths,
        issues=[*parsed.issues, *issues],
        llm_used=True,
    )


def validate_candidate_paths(candidate: AssetAnswerCandidate) -> PathValidationResult:
    """Validate an explicitly assigned candidate without content discovery."""

    if candidate.role == "no_hic":
        return PathValidationResult(paths=[], issues=[])
    supported_suffixes = (
        _ONT_SUFFIXES
        if candidate.role in {"hifi_reads", "ont_reads"}
        else _FASTQ_SUFFIXES
    )
    resolved: list[str] = []
    issues: list[str] = []
    for raw_path in candidate.paths:
        path = Path(raw_path).expanduser().resolve(strict=False)
        if not path.exists():
            issues.append(f"{candidate.role}: file does not exist: {raw_path}")
            continue
        if not path.is_file():
            issues.append(f"{candidate.role}: path is not a regular file: {raw_path}")
            continue
        if not os.access(path, os.R_OK):
            issues.append(f"{candidate.role}: file is not readable: {raw_path}")
            continue
        try:
            if path.stat().st_size <= 0:
                issues.append(f"{candidate.role}: file is empty: {raw_path}")
                continue
        except OSError as exc:
            issues.append(f"{candidate.role}: could not stat {raw_path}: {exc}")
            continue
        suffix = _compound_suffix(path)
        if suffix not in supported_suffixes:
            issues.append(
                f"{candidate.role}: unsupported read-file suffix for {raw_path}; "
                f"expected one of {', '.join(supported_suffixes)}"
            )
            continue
        resolved.append(str(path))
    return PathValidationResult(paths=resolved, issues=issues)


def apply_asset_answer(
    draft: TuiSessionDraft,
    *,
    answer: str,
    focused_role: IntakeRole | None,
    llm_parser: Callable[
        [str, IntakeRole | None],
        Sequence[AssetAnswerCandidate | Mapping[str, Any]] | Mapping[str, Any] | str,
    ]
    | None = None,
    replace_confirmed: bool = False,
) -> AssetAnswerApplyResult:
    """Validate and merge one answer into a draft without silently replacing facts."""

    parsed = parse_asset_answer_with_fallback(
        answer,
        focused_role=focused_role,
        llm_parser=llm_parser,
    )
    working = draft.model_copy(deep=True)
    intake = working.intake.model_copy(deep=True)
    issues = list(parsed.issues)
    accepted_roles: list[str] = []
    unresolved = list(intake.unresolved_candidates)

    no_hic_candidates = [
        candidate for candidate in parsed.candidates if candidate.role == "no_hic"
    ]
    path_candidates = [
        candidate for candidate in parsed.candidates if candidate.role != "no_hic"
    ]
    if no_hic_candidates and any(
        candidate.role.startswith("hic_") for candidate in path_candidates
    ):
        issues.append(
            "one answer cannot both provide Hi-C reads and state that no Hi-C exists"
        )
        no_hic_candidates = []

    validated_by_role: dict[str, list[tuple[AssetAnswerCandidate, list[str]]]] = {
        "hifi_reads": [],
        "ont_reads": [],
        "hic_r1": [],
        "hic_r2": [],
    }
    for candidate in path_candidates:
        validation = validate_candidate_paths(candidate)
        if not validation.valid:
            issues.extend(validation.issues)
            unresolved.append(
                UnresolvedAssetCandidate(
                    candidate=candidate,
                    reason="local path validation failed",
                )
            )
            continue
        normalized_candidate = candidate.model_copy(
            update={"paths": validation.paths, "source_spans": []}
        )
        validated_by_role[candidate.role].append(
            (normalized_candidate, validation.paths)
        )

    hifi_paths = _flatten_candidate_paths(validated_by_role["hifi_reads"])
    if hifi_paths:
        new_hifi = HifiReadsFact(paths=hifi_paths)
        existing_hifi = intake.hifi_reads
        if existing_hifi is not None and existing_hifi.paths != new_hifi.paths:
            if replace_confirmed:
                intake.hifi_reads = new_hifi
                accepted_roles.append("hifi_reads")
                unresolved = [
                    item
                    for item in unresolved
                    if item.reason != "would replace confirmed HiFi reads"
                ]
            else:
                issues.append(
                    "HiFi reads are already confirmed; explicitly confirm "
                    "replacement before changing them"
                )
                unresolved.extend(
                    UnresolvedAssetCandidate(
                        candidate=candidate,
                        reason="would replace confirmed HiFi reads",
                    )
                    for candidate, _paths in validated_by_role["hifi_reads"]
                )
        else:
            intake.hifi_reads = new_hifi
            accepted_roles.append("hifi_reads")

    ont_paths = _flatten_candidate_paths(validated_by_role["ont_reads"])
    if ont_paths:
        new_ont = OntReadsFact(paths=ont_paths)
        existing_ont = intake.ont_reads
        if existing_ont is not None and existing_ont.paths != new_ont.paths:
            if replace_confirmed:
                intake.ont_reads = new_ont
                accepted_roles.append("ont_reads")
                unresolved = [
                    item
                    for item in unresolved
                    if item.reason != "would replace confirmed ONT reads"
                ]
            else:
                issues.append(
                    "ONT reads are already confirmed; explicitly confirm replacement "
                    "before changing them"
                )
                unresolved.extend(
                    UnresolvedAssetCandidate(
                        candidate=candidate, reason="would replace confirmed ONT reads"
                    )
                    for candidate, _paths in validated_by_role["ont_reads"]
                )
        else:
            intake.ont_reads = new_ont
            accepted_roles.append("ont_reads")

    if no_hic_candidates:
        if isinstance(intake.hic_reads, HiCPairedReadsFact) and not replace_confirmed:
            issues.append(
                "Hi-C pairs are already confirmed; explicitly confirm replacement "
                "before recording no Hi-C"
            )
            unresolved.extend(
                UnresolvedAssetCandidate(
                    candidate=candidate,
                    reason="would replace confirmed Hi-C pairs",
                )
                for candidate in no_hic_candidates
            )
        else:
            intake.hic_reads = NoHiCFact()
            working.degradation_decision = None
            accepted_roles.append("no_hic")
            unresolved = [
                item
                for item in unresolved
                if item.candidate.role not in {"hic_r1", "hic_r2"}
            ]
    else:
        _merge_hic_candidates(
            intake=intake,
            unresolved=unresolved,
            validated_r1=validated_by_role["hic_r1"],
            validated_r2=validated_by_role["hic_r2"],
            replace_confirmed=replace_confirmed,
            accepted_roles=accepted_roles,
            issues=issues,
            draft=working,
        )

    record = IntakeAnswerRecord(
        raw_answer=answer,
        focused_role=focused_role,
        candidates=parsed.candidates,
        accepted_roles=accepted_roles,
        issues=issues,
        llm_used=parsed.llm_used,
    )
    intake.answer_history.append(record)
    intake.unresolved_candidates = unresolved
    updated = working.model_copy(update={"intake": intake})
    return AssetAnswerApplyResult(
        draft=updated,
        accepted_roles=accepted_roles,
        issues=issues,
        unresolved_candidates=unresolved,
        llm_used=parsed.llm_used,
    )


def materialize_manifest_draft(draft: TuiSessionDraft) -> InputManifest:
    """Build the mutable manifest projection from confirmed intake facts only."""

    assets: list[InputAssetSpec] = []
    if draft.intake.hifi_reads is not None:
        hifi_paths = draft.intake.hifi_reads.paths
        assets.append(
            InputAssetSpec(
                asset_type="raw_long_reads",
                paths=hifi_paths,
                sample_id=draft.intent.sample_id,
                platform="pacbio_hifi",
                format=_shared_ont_format(hifi_paths),
                metadata={"intake_role": "hifi_reads"},
            )
        )
    if draft.intake.ont_reads is not None:
        ont_paths = draft.intake.ont_reads.paths
        assets.append(
            InputAssetSpec(
                asset_type="raw_long_reads",
                paths=ont_paths,
                sample_id=draft.intent.sample_id,
                platform="ont",
                format=_shared_ont_format(ont_paths),
                metadata={"intake_role": "ont_reads"},
            )
        )
    if isinstance(draft.intake.hic_reads, HiCPairedReadsFact):
        pairs = [
            PairedReadSpec(r1=pair.r1, r2=pair.r2)
            for pair in draft.intake.hic_reads.pairs
        ]
        assets.append(
            InputAssetSpec(
                asset_type="hic_reads_registered",
                sample_id=draft.intent.sample_id,
                platform="illumina",
                layout="paired",
                format="fastq",
                paired_reads=pairs,
                metadata={"intake_role": "hic_reads"},
            )
        )
    metadata: dict[str, Any] = {}
    if draft.intent.target_species:
        metadata["target_species"] = draft.intent.target_species
    if isinstance(draft.intake.hic_reads, NoHiCFact):
        metadata["hic_status"] = "not_provided"
        if draft.degradation_decision and draft.degradation_decision.confirmed:
            metadata["hic_degradation_confirmed"] = True
    degraded_without_hic = (
        isinstance(draft.intake.hic_reads, NoHiCFact)
        and draft.degradation_decision is not None
        and draft.degradation_decision.confirmed
    )
    genome_size_estimate = (
        draft.shared_parameters.estimated_genome_size
        if draft.shared_parameters is not None
        else None
    )
    return InputManifest(
        session_id=draft.session_id,
        sample_id=draft.intent.sample_id,
        assets=assets,
        user_metadata=metadata,
        target_assembly_level=(
            "contig" if degraded_without_hic else draft.intent.target_assembly_level
        ),
        genome_size_estimate=genome_size_estimate,
    )


def create_rerun_tui_session_draft(
    parent: TuiSessionDraft,
    *,
    session_id: str,
    request_text: str | None = None,
    intent: WorkflowIntent | None = None,
) -> TuiSessionDraft:
    """Create an editable child draft without mutating a completed parent.

    Previously confirmed paths are reused only after the same bounded local
    validation used during intake.  Missing or changed files are retained as
    unresolved candidates, so the child asks for that role again instead of
    inheriting a stale fact.  Planning choices are deliberately reset: a rerun
    may change its goal, stages, methods, or parameters.
    """

    child_text = (request_text or parent.request_text).strip()
    child_intent = intent or deterministic_workflow_intent(child_text)
    child = create_tui_session_draft(
        session_id=session_id,
        data_dir=parent.data_dir,
        request_text=child_text,
        intent=child_intent,
        parent_session_id=parent.session_id,
    )
    intake = child.intake.model_copy(deep=True)
    unresolved: list[UnresolvedAssetCandidate] = []

    if parent.intake.hifi_reads is not None:
        candidate = AssetAnswerCandidate(
            role="hifi_reads",
            paths=list(parent.intake.hifi_reads.paths),
            explicit_role=True,
        )
        validation = validate_candidate_paths(candidate)
        if validation.valid:
            intake.hifi_reads = HifiReadsFact(paths=validation.paths)
        else:
            unresolved.append(
                UnresolvedAssetCandidate(
                    candidate=candidate,
                    reason="reused HiFi paths no longer pass local validation",
                )
            )

    if parent.intake.ont_reads is not None:
        candidate = AssetAnswerCandidate(
            role="ont_reads",
            paths=list(parent.intake.ont_reads.paths),
            explicit_role=True,
        )
        validation = validate_candidate_paths(candidate)
        if validation.valid:
            intake.ont_reads = OntReadsFact(paths=validation.paths)
        else:
            unresolved.append(
                UnresolvedAssetCandidate(
                    candidate=candidate,
                    reason="reused ONT paths no longer pass local validation",
                )
            )

    parent_hic = parent.intake.hic_reads
    if isinstance(parent_hic, HiCPairedReadsFact):
        valid_pairs: list[HiCReadPair] = []
        valid_pair_candidates: list[
            tuple[AssetAnswerCandidate, AssetAnswerCandidate]
        ] = []
        hic_valid = True
        for pair in parent_hic.pairs:
            r1_candidate = AssetAnswerCandidate(
                role="hic_r1", paths=[pair.r1], explicit_role=True
            )
            r2_candidate = AssetAnswerCandidate(
                role="hic_r2", paths=[pair.r2], explicit_role=True
            )
            r1_validation = validate_candidate_paths(r1_candidate)
            r2_validation = validate_candidate_paths(r2_candidate)
            if not r1_validation.valid or not r2_validation.valid:
                hic_valid = False
                unresolved.extend(
                    [
                        UnresolvedAssetCandidate(
                            candidate=r1_candidate,
                            reason=(
                                "reused Hi-C R1 path no longer passes local validation"
                            ),
                        ),
                        UnresolvedAssetCandidate(
                            candidate=r2_candidate,
                            reason=(
                                "reused Hi-C R2 path no longer passes local validation"
                            ),
                        ),
                    ]
                )
                continue
            valid_pairs.append(
                HiCReadPair(
                    r1=r1_validation.paths[0],
                    r2=r2_validation.paths[0],
                )
            )
            valid_pair_candidates.append((r1_candidate, r2_candidate))
        if hic_valid and valid_pairs:
            intake.hic_reads = HiCPairedReadsFact(pairs=valid_pairs)
        elif not hic_valid:
            # All-or-nothing: move any previously valid pairs to unresolved too.
            for r1_cand, r2_cand in valid_pair_candidates:
                unresolved.extend(
                    [
                        UnresolvedAssetCandidate(
                            candidate=r1_cand,
                            reason=(
                                "reused Hi-C R1 path no longer passes local validation"
                            ),
                        ),
                        UnresolvedAssetCandidate(
                            candidate=r2_cand,
                            reason=(
                                "reused Hi-C R2 path no longer passes local validation"
                            ),
                        ),
                    ]
                )
    elif isinstance(parent_hic, NoHiCFact):
        intake.hic_reads = NoHiCFact()

    intake.unresolved_candidates = unresolved
    return child.model_copy(update={"intake": intake})


def save_tui_session_draft(draft: TuiSessionDraft) -> tuple[Path, Path]:
    """Persist state and its rebuildable manifest projection with atomic replaces.

    ``tui_state.json`` is the authority.  The projection is published first so
    a failed save can only leave a stale projection beside the previous state;
    the next successful save rebuilds it from the authoritative draft.

    A compare-and-swap on ``revision`` prevents a stale in-memory draft from
    silently overwriting a newer on-disk version.  On a successful write the
    ``draft`` object's ``revision`` field is incremented in place so callers
    holding the same reference observe the new value without reloading.
    """

    paths = SessionPaths(draft.data_dir, draft.session_id)
    ensure_run_layout(paths)
    state_path = paths.tui_state_path
    draft_path = paths.manifest_draft_path

    if state_path.exists():
        try:
            on_disk = json.loads(state_path.read_text(encoding="utf-8"))
            on_disk_revision = int(on_disk.get("revision", 1))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            on_disk_revision = draft.revision
        if on_disk_revision != draft.revision:
            raise TuiDraftConflictError(
                f"TUI draft for session {draft.session_id!r} was modified by "
                f"another writer (on-disk revision {on_disk_revision}, "
                f"this draft revision {draft.revision})"
            )
        new_revision = draft.revision + 1
    else:
        new_revision = draft.revision

    draft.revision = new_revision

    manifest = materialize_manifest_draft(draft)
    manifest_text = yaml.safe_dump(
        manifest.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    )
    state_text = (
        json.dumps(draft.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    )
    _atomic_write_text(draft_path, manifest_text)
    _atomic_write_text(state_path, state_text)
    return state_path, draft_path


def load_tui_session_draft(data_dir: str | Path, session_id: str) -> TuiSessionDraft:
    """Load the authoritative mutable TUI session draft."""

    requested_data_dir = str(Path(data_dir).expanduser().resolve())
    state_path = SessionPaths(requested_data_dir, session_id).tui_state_path
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"TUI draft not found for session {session_id!r} under "
            f"data_dir {requested_data_dir!r}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"TUI draft is not valid JSON: {state_path}: {exc}") from exc
    try:
        draft = TuiSessionDraft.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"TUI draft failed validation: {exc}") from exc
    if draft.session_id != session_id:
        raise ValueError(
            f"TUI draft session_id {draft.session_id!r} does not match requested "
            f"session {session_id!r}"
        )
    if draft.data_dir != requested_data_dir:
        raise ValueError(
            f"TUI draft data_dir {draft.data_dir!r} does not match requested "
            f"data_dir {requested_data_dir!r}"
        )
    return draft


def _coerce_workflow_intent(
    value: WorkflowIntent | Mapping[str, Any] | str,
) -> WorkflowIntent:
    if isinstance(value, WorkflowIntent):
        return value
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("LLM workflow intent must be an object")
    return WorkflowIntent.model_validate(value)


def _coerce_llm_candidates(
    value: Sequence[AssetAnswerCandidate | Mapping[str, Any]] | Mapping[str, Any] | str,
) -> list[AssetAnswerCandidate]:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, Mapping):
        value = value.get("candidates", value.get("assets", []))
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("LLM asset answer must contain a candidate list")
    return [
        item
        if isinstance(item, AssetAnswerCandidate)
        else AssetAnswerCandidate.model_validate(item)
        for item in value
    ]


def _safe_llm_candidates(
    candidates: Sequence[AssetAnswerCandidate],
    *,
    raw_answer: str,
    deterministic: ParsedAssetAnswer,
) -> tuple[list[AssetAnswerCandidate], list[str]]:
    """Admit model candidates only when local textual evidence proves them."""

    raw_paths = {span.path for span in deterministic.path_spans}
    admitted_by_role: dict[str, set[str]] = {
        "hifi_reads": set(),
        "ont_reads": set(),
        "hic_r1": set(),
        "hic_r2": set(),
    }
    for candidate in deterministic.candidates:
        if candidate.role in admitted_by_role:
            admitted_by_role[candidate.role].update(candidate.paths)
    no_hic_explicit = bool(
        _NO_HIC_RE.search(_mask_path_spans(raw_answer, deterministic.path_spans))
    )

    accepted: list[AssetAnswerCandidate] = []
    issues: list[str] = []
    for candidate in candidates:
        if candidate.role == "no_hic":
            if no_hic_explicit:
                accepted.append(candidate)
            else:
                issues.append(
                    "ignored LLM no_hic candidate without an explicit user statement"
                )
            continue
        missing_paths = [path for path in candidate.paths if path not in raw_paths]
        if missing_paths:
            issues.append(
                "ignored LLM candidate with paths absent from the user answer: "
                + ", ".join(missing_paths)
            )
            continue
        if not set(candidate.paths).issubset(admitted_by_role[candidate.role]):
            issues.append(
                f"ignored LLM {candidate.role} candidate without deterministic "
                "role evidence"
            )
            continue
        accepted.append(candidate)
    return accepted, issues


def _merge_candidates(
    base: Sequence[AssetAnswerCandidate], additions: Sequence[AssetAnswerCandidate]
) -> list[AssetAnswerCandidate]:
    grouped: dict[str, list[str]] = {}
    explicit: dict[str, bool] = {}
    spans: dict[str, list[AssetPathSpan]] = {}
    for candidate in [*base, *additions]:
        role = candidate.role
        if role == "no_hic":
            grouped[role] = []
            explicit[role] = explicit.get(role, False) or candidate.explicit_role
            spans[role] = []
            continue
        grouped.setdefault(role, [])
        spans.setdefault(role, [])
        for index, path in enumerate(candidate.paths):
            if path not in grouped[role]:
                grouped[role].append(path)
                if candidate.source_spans:
                    spans[role].append(candidate.source_spans[index])
        explicit[role] = explicit.get(role, False) or candidate.explicit_role
    return [
        AssetAnswerCandidate(
            role=role,
            paths=paths,
            explicit_role=explicit.get(role, False),
            source_spans=spans.get(role, []),
        )
        for role, paths in grouped.items()
        if role == "no_hic" or paths
    ]


def _mask_path_spans(answer: str, spans: Sequence[AssetPathSpan]) -> str:
    characters = list(answer)
    for span in spans:
        for index in range(span.start, min(span.end, len(characters))):
            characters[index] = " "
    return "".join(characters)


def _role_for_path_span(
    *,
    span: AssetPathSpan,
    masked_answer: str,
    focused_role: IntakeRole | None,
) -> tuple[
    Literal["hifi_reads", "ont_reads", "hic_r1", "hic_r2"] | None,
    bool,
]:
    clause_start, clause_end = _clause_bounds(masked_answer, span.start)
    clause = masked_answer[clause_start:clause_end]
    clause_has_hic = bool(_HIC_LABEL_RE.search(clause))
    has_hic = clause_has_hic or bool(
        _HIC_LABEL_RE.search(masked_answer)
    )
    explicit_role = _nearest_explicit_role_before(
        clause,
        offset=clause_start,
        path_start=span.start,
    )
    if explicit_role == "hic_unpaired":
        return None, False
    if explicit_role == "hifi_reads":
        return "hifi_reads", True
    if explicit_role == "ont_reads":
        return "ont_reads", True
    if explicit_role in {"hic_r1", "hic_r2"} and (
        has_hic or focused_role == "hic_reads"
    ):
        return explicit_role, True
    if clause_has_hic:
        return None, False
    if focused_role == "hifi_reads":
        return "hifi_reads", False
    if focused_role == "ont_reads":
        return "ont_reads", False
    return None, False


def _clause_bounds(text: str, position: int) -> tuple[int, int]:
    start = 0
    end = len(text)
    for match in _CLAUSE_BOUNDARY_RE.finditer(text):
        if match.end() <= position:
            start = match.end()
        elif match.start() >= position:
            end = match.start()
            break
    return start, end


def _nearest_explicit_role_before(
    clause: str,
    *,
    offset: int,
    path_start: int,
) -> Literal[
    "hifi_reads",
    "ont_reads",
    "hic_r1",
    "hic_r2",
    "hic_unpaired",
] | None:
    """Return the closest role label before the path in the same clause.

    Looking only backwards prevents a later ``R1`` label from stealing an
    earlier ONT path in a mixed-role sentence.  The TUI prompt examples all use
    label-then-path phrasing, and ambiguity should remain unclassified rather
    than be guessed.
    """

    markers: list[
        tuple[
            int,
            int,
            Literal[
                "hifi_reads",
                "ont_reads",
                "hic_r1",
                "hic_r2",
                "hic_unpaired",
            ],
        ]
    ] = []
    markers.extend(
        (offset + match.start(), offset + match.end(), "hifi_reads")
        for match in _HIFI_LABEL_RE.finditer(clause)
    )
    markers.extend(
        (offset + match.start(), offset + match.end(), "ont_reads")
        for match in _ONT_LABEL_RE.finditer(clause)
    )
    markers.extend(
        (offset + match.start(), offset + match.end(), "hic_r1")
        for match in _R1_LABEL_RE.finditer(clause)
    )
    markers.extend(
        (offset + match.start(), offset + match.end(), "hic_r2")
        for match in _R2_LABEL_RE.finditer(clause)
    )
    markers.extend(
        (offset + match.start(), offset + match.end(), "hic_unpaired")
        for match in _HIC_LABEL_RE.finditer(clause)
    )
    preceding = [marker for marker in markers if marker[1] <= path_start]
    if not preceding:
        return None
    return max(preceding, key=lambda marker: marker[1])[2]


def _flatten_candidate_paths(
    candidates: Sequence[tuple[AssetAnswerCandidate, list[str]]],
) -> list[str]:
    paths: list[str] = []
    for _candidate, values in candidates:
        for path in values:
            if path not in paths:
                paths.append(path)
    return paths


def _merge_hic_candidates(
    *,
    intake: AssetCollectionState,
    unresolved: list[UnresolvedAssetCandidate],
    validated_r1: Sequence[tuple[AssetAnswerCandidate, list[str]]],
    validated_r2: Sequence[tuple[AssetAnswerCandidate, list[str]]],
    replace_confirmed: bool,
    accepted_roles: list[str],
    issues: list[str],
    draft: TuiSessionDraft,
) -> None:
    new_r1 = _flatten_candidate_paths(validated_r1)
    new_r2 = _flatten_candidate_paths(validated_r2)
    if not new_r1 and not new_r2:
        return
    if isinstance(intake.hic_reads, NoHiCFact):
        if not replace_confirmed:
            issues.append(
                "no Hi-C is already confirmed; explicitly confirm replacement "
                "before adding Hi-C reads"
            )
            unresolved.extend(
                UnresolvedAssetCandidate(
                    candidate=candidate, reason="would replace no_hic fact"
                )
                for candidate, _paths in [*validated_r1, *validated_r2]
            )
            return
        intake.hic_reads = None
        draft.degradation_decision = None

    if isinstance(intake.hic_reads, HiCPairedReadsFact) and not replace_confirmed:
        issues.append(
            "Hi-C pairs are already confirmed; explicitly confirm replacement "
            "before changing them"
        )
        unresolved.extend(
            UnresolvedAssetCandidate(
                candidate=candidate,
                reason="would replace confirmed Hi-C pairs",
            )
            for candidate, _paths in [*validated_r1, *validated_r2]
        )
        return
    if replace_confirmed:
        unresolved[:] = [
            item
            for item in unresolved
            if item.reason != "would replace confirmed Hi-C pairs"
        ]

    pending_r1, pending_r2, retained, pending_items = _pending_hic_mates(unresolved)
    combined_r1 = [*pending_r1, *new_r1]
    combined_r2 = [*pending_r2, *new_r2]
    if combined_r1 and len(combined_r1) == len(combined_r2):
        try:
            pairs = [
                HiCReadPair(r1=r1, r2=r2)
                for r1, r2 in zip(combined_r1, combined_r2, strict=True)
            ]
            intake.hic_reads = HiCPairedReadsFact(pairs=pairs)
        except (ValueError, ValidationError) as exc:
            issues.append(f"could not accept Hi-C pair: {exc}")
            unresolved[:] = retained
            unresolved.extend(pending_items)
            unresolved.extend(
                UnresolvedAssetCandidate(
                    candidate=candidate, reason="invalid Hi-C pair"
                )
                for candidate, _paths in [*validated_r1, *validated_r2]
            )
            return
        unresolved[:] = retained
        accepted_roles.append("hic_reads")
        return

    unresolved[:] = retained
    if combined_r1 and not combined_r2:
        unresolved.extend(pending_items)
        unresolved.extend(
            UnresolvedAssetCandidate(
                candidate=candidate, reason=_PENDING_HIC_MATE_REASON
            )
            for candidate, _paths in validated_r1
        )
        issues.append(
            "Hi-C R1 was validated; provide one explicitly labelled R2 "
            "to confirm the pair"
        )
        return
    if combined_r2 and not combined_r1:
        unresolved.extend(pending_items)
        unresolved.extend(
            UnresolvedAssetCandidate(
                candidate=candidate, reason=_PENDING_HIC_MATE_REASON
            )
            for candidate, _paths in validated_r2
        )
        issues.append(
            "Hi-C R2 was validated; provide one explicitly labelled R1 "
            "to confirm the pair"
        )
        return
    unresolved.extend(pending_items)
    unresolved.extend(
        UnresolvedAssetCandidate(
            candidate=candidate,
            reason=(
                "Hi-C R1/R2 lane counts do not match"
            ),
        )
        for candidate, _paths in [*validated_r1, *validated_r2]
    )
    issues.append(
        "Hi-C R1/R2 lane counts do not match; provide the same number of "
        "explicitly labelled R1 and R2 files in corresponding order"
    )


def _pending_hic_mates(
    unresolved: Sequence[UnresolvedAssetCandidate],
) -> tuple[
    list[str],
    list[str],
    list[UnresolvedAssetCandidate],
    list[UnresolvedAssetCandidate],
]:
    r1: list[str] = []
    r2: list[str] = []
    retained: list[UnresolvedAssetCandidate] = []
    pending_items: list[UnresolvedAssetCandidate] = []
    for item in unresolved:
        if item.reason != _PENDING_HIC_MATE_REASON:
            retained.append(item)
            continue
        if item.candidate.role == "hic_r1":
            r1.extend(item.candidate.paths)
            pending_items.append(item)
        elif item.candidate.role == "hic_r2":
            r2.extend(item.candidate.paths)
            pending_items.append(item)
        else:
            retained.append(item)
    return r1, r2, retained, pending_items


def _compound_suffix(path: Path) -> str:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if len(suffixes) >= 2 and suffixes[-1] == ".gz":
        return "".join(suffixes[-2:])
    return path.suffix.lower()


def _shared_ont_format(paths: Sequence[str]) -> Literal["fastq", "fasta", "unknown"]:
    suffixes = {_compound_suffix(Path(path)) for path in paths}
    if suffixes and suffixes.issubset(set(_FASTQ_SUFFIXES)):
        return "fastq"
    if suffixes and suffixes.issubset(set(_FASTA_SUFFIXES)):
        return "fasta"
    return "unknown"


def _extract_target_species(text: str) -> str | None:
    for pattern in _TARGET_SPECIES_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group("species").strip().strip("""'\""“”‘’""")
            if value:
                return value
    return None


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
