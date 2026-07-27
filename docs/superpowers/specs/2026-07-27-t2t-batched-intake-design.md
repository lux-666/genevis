# T2T Batched Intake and Deferred Motif Design

## Status and Scope

This change extends the existing non-annotation TUI intake so an operator can
start a real T2T run by supplying ONT reads first and then completing the
remaining biological and sequencing facts in one answer. It also makes the
TUI's route selection use the repository's existing canonical stage order, so
`assembly_dedup` is present in the default T2T and assembly-only routes
without introducing a second stage-order table.

The work is limited to TUI intake, its durable draft, the projection into
shared parameters, and the default assembly-oriented stage sequences. It does
not change bootstrap, annotation, package installation, remote execution, HPC
submission, species taxonomy lookup, or the existing T2T motif resolver.

## Goals

- Ask for all currently missing T2T facts together rather than one field per
  turn.
- Accept ONT, Hi-C, species, and an optional telomere motif from the same user
  answer when they are explicitly identified.
- Preserve the user's species label verbatim; `human`, `人类`, and
  `Homo sapiens` are all valid values and are not normalized or mapped to a
  taxon.
- Treat a supplied telomere motif as an explicit override and an omitted or
  unknown motif as deferred to the existing T2T LLM resolver.
- Continue asking for missing Hi-C or species facts required by T2T, while
  never asking again for an optional motif that has already been offered.
- Let the operator switch to the complete assembly-only route instead of
  supplying T2T-only facts.
- Keep exact-path validation, explicit Hi-C mate attribution, replacement
  confirmation, frozen-input behavior, and bootstrap ownership unchanged.

## Workflow Routes

The user-visible route contracts are:

```text
read_qc
  -> genome_survey
  -> assembly
  -> assembly_dedup
  -> assembly_review
  -> hic_scaffolding
  -> t2t
```

The assembly-only route becomes:

```text
read_qc
  -> genome_survey
  -> assembly
  -> assembly_dedup
  -> assembly_review
```

An explicit request such as `改为只做 assembly` or `不开 T2T` switches the
draft to the assembly-only route, sets the target assembly level to `contig`,
and removes `hic_scaffolding` and `t2t` from the requested scope. Hi-C,
species, and motif then cease to be intake requirements. Previously entered
facts remain auditable in the draft but do not restore the removed stages or
cause those stages to execute.

The lists above are documentation examples, not independently persisted
configuration. Route expansion is derived from the existing
`app.workflow.STAGE_ORDER`, whose canonical order already contains
`assembly_dedup` between `assembly` and `assembly_review`. The implementation
adds one small helper, conceptually `stages_through(endpoint)`, which returns
the non-`inventory` prefix through an endpoint:

```text
stages_through("t2t")
  -> read_qc ... assembly_dedup assembly_review hic_scaffolding t2t

stages_through("assembly_review")
  -> read_qc ... assembly_dedup assembly_review
```

The helper's expanded list remains compatible with the existing
`requested_stages` field and frozen run-config contract. The TUI stores the
expanded result, while graph routing and planner ordering continue to consume
the existing field. No new graph edge, second stage registry, or route-specific
literal stage array is introduced.

## Existing Implementation Refactor vs New Behavior

The following pieces already exist and are reused rather than rebuilt:

| Existing implementation | Change in this work |
|---|---|
| `app.workflow.STAGE_ORDER` already defines the canonical stage order, including `assembly_dedup` | Add the endpoint-prefix helper and make it the route-expansion source |
| `app.graph.builder` registers every stage and `app.graph.routing` skips stages not in `requested_stages` | Keep graph construction and routing unchanged |
| `ask_planner._resolve_execution_order` orders a selected set through `STAGE_ORDER` | Keep it as the planner ordering boundary |
| `ask_planner._recommended_stages_from_manifest` recommends an assembly prefix and downstream stages | Replace its duplicated assembly-prefix list with the same helper; preserve its asset-based downstream recommendations |
| `tui_intake.deterministic_workflow_intent` currently writes literal stage arrays | Refactor it to choose a route endpoint and call the shared helper |
| `tui._effective_requested_stages` removes Hi-C/T2T after an existing degradation decision | Route downgrade calls the assembly endpoint helper; no second hand-maintained removal list is needed |

New behavior is limited to batched biological intake, optional motif
deferral, species projection, explicit assembly-only switching, and tests for
the route-expansion helper. Adding `assembly_dedup` to `STAGE_ORDER`, graph
registration, or graph routing is not part of this feature because those
pieces already exist.

## Batched Intake Conversation

The first focused prompt may still ask for ONT reads because they are the
minimum supported starting asset. An answer may nevertheless contain other
explicitly labelled facts, and the parser continues to process every fact in
the answer rather than only the focused role.

After ONT is known, a T2T draft with no other facts renders one batch such as:

```text
还缺少 T2T 所需信息，请按类似格式一次提供：

Hi-C：R1="/data/hic_1.fastq.gz"，R2="/data/hic_2.fastq.gz"
物种："人类"
motif（可选）："TTAGGG"

也可以回复“改为只做 assembly”。
```

The batch is derived from current requirements and confirmed facts:

- missing Hi-C is shown whenever the active route contains
  `hic_scaffolding` or `t2t`;
- missing species is shown whenever the active route contains `t2t`;
- optional motif is shown until the operator has made a valid optional-motif
  choice; and
- later prompts show every still-missing required fact together.

A response may complete any subset of the displayed facts. Accepted facts are
persisted atomically, and the next prompt is regenerated from the updated
draft. A partial answer such as only Hi-C therefore leads to a species-only
follow-up, not another Hi-C question and not another motif offer.

## Required and Deferred Facts

ONT, Hi-C, and species have different route-dependent requirements:

| Fact | T2T route | Assembly-only route |
|---|---|---|
| ONT reads | required | required |
| Hi-C reads | required | not required |
| species label | required | not required |
| telomere motif | optional, user override | not requested |

Hi-C and species remain unresolved when omitted from an answer and are
included in the next batch. The user may resolve those missing requirements by
providing them or by explicitly switching to the assembly-only route.

Motif follows the same source-priority concept as genome size:

| Parameter | Explicit value | No explicit value |
|---|---|---|
| genome size | operator value overrides survey-derived size | await executable `genome_survey` |
| telomere motif | operator value bypasses motif LLM resolution | await T2T motif resolver using species |

Omitting motif and explicitly saying that the motif is unknown have the same
effect. After any non-empty batch answer, an absent motif label or an explicit
unknown motif records that the optional choice is deferred. A valid supplied
motif records the supplied choice. In both cases, motif is not displayed again.
No string such as `unknown` or `不知道` is stored in
`SpeciesFacts.telomere_motif`. The plan preview may describe a deferred value
as `auto: await T2T motif resolver`, analogous to the existing
`awaiting_genome_survey` presentation.

No generic tri-state parameter system is introduced. A single durable
motif-choice-complete flag records whether the optional value has been
supplied or deferred. An invalid motif does not set the flag and therefore
appears again as a correction item. Required facts continue to be derived from
route plus confirmed values, as they are today.

The intake draft may receive a valid motif before it receives a species. That
partial answer is accepted into an intake-only staging field on the workflow
intent. The staging field is not `SpeciesFacts`: it is carried forward until a
species label is available, then one projection helper constructs
`SpeciesFacts` with both values. This preserves the existing non-empty
`scientific_name` contract while allowing any subset of a batch answer.

## Species and Shared-Parameter Ownership

The species value is an operator-supplied label, not a taxonomy record. Intake
may remove surrounding syntax used by the prompt, such as the `物种:` label or
matching quotation marks, but it preserves the value itself without alias
resolution, scientific-name conversion, taxon lookup, or case normalization.

One intake projection helper keeps the conversational facts and the
execution-facing shared fact synchronized:

```text
intent.target_species
intent.telomere_motif              # intake staging; may exist without species
shared_parameters.species_facts.scientific_name
shared_parameters.species_facts.telomere_motif
```

The shared `SpeciesFacts` object is the authority consumed by T2T parameter
resolution. `intent.target_species` remains the raw compatibility fact for
the existing TUI summary and manifest metadata; `intent.telomere_motif` is
retained as the intake staging value until planning projection.
The helper constructs or updates `SharedParameters` only when species exists,
without discarding already collected platform, read type, genome size, or
other species facts.

The projection helper runs when a draft is created and whenever either staged
species or motif changes. Thus a species stated in the initial goal is already
available in `SharedParameters`; a later valid motif updates that projection.
If motif arrives first, it remains staged until the species field becomes
available. This is a derived synchronization, not a second independently
editable species source.

A supplied motif is validated and staged in the workflow intent, then written
to `shared_parameters.species_facts.telomere_motif` during planning. The
existing DNA motif validator remains authoritative. Existing T2T behavior then
applies without a new execution node: a supplied motif bypasses the LLM, while
an absent motif causes the resolver to use the verbatim species label.

## Answer Parsing and Validation

The existing exact-path pipeline remains responsible for ONT and Hi-C facts.
It continues to require paths to occur literally in the raw answer, requires
explicit Hi-C R1/R2 attribution, performs bounded local file validation, and
does not infer roles from filenames.

The batch parser additionally extracts explicitly labelled species and motif
clauses from the same raw answer. Deterministic labelled extraction is
preferred. The existing bounded LLM fallback may identify fields in more
natural phrasing, but it must return source evidence from the raw answer and
must not invent or normalize a species or motif value.

One parsed response can therefore contain:

```text
ONT paths
Hi-C R1/R2 pairs or an explicit no-Hi-C statement
species label
optional telomere motif
assembly-only scope switch
```

Confirmed values retain the current replacement policy. A response that
changes confirmed ONT, Hi-C, species, or motif data requires explicit
replacement confirmation rather than silently overwriting the draft.

When one response includes a direct assembly-only switch plus other facts, the
explicit scope switch wins for stage selection. Valid ONT/Hi-C/species/motif
facts may still be retained in the audit and draft, but only ONT remains
required and none of the T2T-only facts is projected into an executable T2T
scope. A direct switch combined with `没有 Hi-C` does not create a second
degradation confirmation; the direct route choice is confirmation.

## No-Hi-C and Scope Downgrade

An explicit `没有 Hi-C` statement retains the current degradation warning:
the run cannot claim complete Hi-C scaffolding or T2T. Confirmation switches
the requested scope to the assembly-only route defined above. An explicit
`只做 assembly` request is itself a direct scope choice and does not require a
second degradation confirmation.

The effective stage list is written into planning from the updated intent,
not reconstructed only at execution time. The later stage-selection UI may
still edit the plan, but re-adding `hic_scaffolding` or `t2t` causes the same
required-fact checks to apply before confirmation.

## Error Handling

- Invalid or nonexistent paths remain unresolved and are reported without
  discarding valid facts from the same response.
- An invalid motif is rejected with the existing DNA-motif validation error and
  remains a correction item in the next batch. It is not converted to deferred
  resolution. Only omission or an explicit unknown answer marks the optional
  motif as deferred.
- An empty or ambiguous species value remains missing and is asked again while
  T2T remains selected.
- A species label is never rejected merely because it is not a scientific
  name or recognized alias.
- A batch with several facts may accept valid fields and report invalid ones;
  persistence remains one coherent draft update with an audit record.
- Assembly-only scope switching never deletes prior answer history or mutates
  bootstrap/frozen-run state.

## Testing

Focused tests must cover:

1. default T2T stages contain `assembly_dedup` between `assembly` and
   `assembly_review`;
2. the assembly-only route contains read QC, survey, assembly, dedup, and
   review but excludes Hi-C and T2T;
3. after ONT intake, one batch displays missing Hi-C, species, optional motif,
   and the assembly-only alternative;
4. one labelled response can accept Hi-C, species, and motif together;
5. partial responses persist accepted facts and the next batch contains all
   and only the remaining required facts;
6. omitted motif and explicit unknown motif are not stored and are not asked
   again;
7. a motif-only partial answer is retained in intake staging until species is
   supplied, without constructing an incomplete `SpeciesFacts` object;
8. `人类`, `human`, and `Homo sapiens` are preserved verbatim through intake
   into `SpeciesFacts.scientific_name`;
9. a supplied motif reaches `SpeciesFacts.telomere_motif` and the existing T2T
   resolver bypasses the LLM;
10. an absent motif is presented as deferred and the existing resolver receives
   the supplied species label;
11. invalid motif syntax is reported while valid facts from the same answer are
   retained;
12. `没有 Hi-C` plus confirmation and a direct `只做 assembly` response both
    produce the assembly-only route; and
13. existing exact-path, Hi-C pairing, replacement, manifest, planning,
    frozen-input, genome-size deferral, and bootstrap tests remain green.

Verification runs focused TUI intake/planner/T2T tests, `ruff check app tests`,
and the complete unit-test suite.

## Non-goals

- No taxonomy parser, species alias table, NCBI lookup, or automatic taxon ID.
- No generic conversational form engine or general optional-parameter state
  machine.
- No change to T2T motif recommendation prompts or model invocation.
- No annotation dataset intake or annotation-stage selection.
- No bootstrap, environment-installation, or remote-HPC behavior changes.
- No automatic data download or discovery from directories.
