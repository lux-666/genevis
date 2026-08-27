# TUI and Planning

> **Status:** implemented for raw HiFi/ONT and paired Hi-C intake; broader
> midstage intake is planned separately.
> **Read when:** changing conversational intake, Plan Draft behavior,
> whole-run freeze, or draft recovery.  
> **Comes from:** operator goal text, confirmed sample facts, descriptor-derived
> method catalog, and machine configuration maintained manually or with the
> optional [bootstrap assistant](bootstrap.md).
> **Authority:** the frozen RunConfig owns stage occurrence and local-execution
> consent; the Assembly stage owns method identity and method parameters after
> upstream evidence exists. Intake text is a routing hint, never an execution
> authority.
> **Feeds into:** immutable frozen inputs and
> [runtime orchestration](runtime-orchestration.md).  
> **Contract dependencies:** [run data](../reference/data-contract.md),
> [descriptors](../reference/descriptors.md), and
> [stage contracts](../reference/stage-contracts.md).  
> **Code evidence:** `app/entrypoints/tui.py`, `app/entrypoints/tui_intake.py`,
> `app/entrypoints/ask_planner.py`, `app/entrypoints/planner_catalog.py`,
> `app/schemas/tui_intake.py`, and `app/storage/frozen_inputs.py`.

## Mutable draft boundary

`gene-agent tui` is an optional conversational new-run entry. Before
confirmation it owns a versioned mutable draft with this lifecycle:

```text
intake -> planning -> ready_to_confirm -> frozen
                                      -> running
                                      -> blocked_review | terminal
```

The draft is not a graph checkpoint and is not an executable contract. It can
be revised or resumed while unfrozen. The only top-level TUI command during
normal conversation is `/resume`, which selects an existing draft or blocked
session through the appropriate recovery path.

For real/operator runs with already prepared files, use the deterministic
boundary instead: `gene-agent run --manifest PATH --run-config PATH --data-dir
PATH`. It treats those files as the settled draft and converges on the same
whole-run freeze and executor. Exact public RunConfig, occurrence, authorization,
and review-policy fields belong to the [run-data contract](../reference/data-contract.md).
An unfinished Plan Draft may still use `None` before stage selection; this is
draft incompleteness, not a second executable runtime meaning.

Current intake directly models PacBio HiFi reads, ONT reads, explicit paired
Hi-C lanes, an explicit no-Hi-C fact, species facts, and a telomere motif when
supplied. It does not currently collect clean long reads, an existing assembly,
CLR, CycloneSEQ, or short reads. Backend manifest and inventory support must
not be mistaken for TUI support; see the
[planned midstage node](tui-midstage-entry.md).

## Intent is not the execution plan

The intent parser decides which facts the TUI still needs to ask about. It does
not authorize tools or own the final stage set. Once intake is complete, the
planner receives the confirmed manifest view and a method catalog derived from
the descriptor registry. It produces the editable Plan Draft for stages and
attention items. Fixed-stage methods come from the registry's internal defaults
rather than a user method field. Assembly is deferred: its stage later
recommends and confirms `assembly_selection` after upstream QC/survey evidence,
then recommends parameters for the bound method.

Intent parsing may recognize a named assembly tool as a transient asset-role
hint only. It asks whether HiFi, ONT, or both roles should be collected; the
hint is not a durable `WorkflowIntent` field and must not create a second
execution path, preselect `assembly_selection`, or feed a method name to the
Plan Draft model. Legacy durable intake documents that still contain a method
field fail schema validation rather than being silently reinterpreted.

Assembly Review is a recommendation, not a completion rule. The default
Assembly recommendation may append `assembly_dedup -> assembly_review`. The
canonical complete-genome/T2T occurrence sequence is owned by
[stage contracts](../reference/stage-contracts.md) and generated for planners by
`app.workflow.recommended_workflow_template`; this topic does not maintain a
second literal array.

The final Review is a second occurrence of the reusable stage. With the current
Base Polish placeholder, freeze binds that otherwise unreachable Review to T2T,
the nearest implemented assembly-handoff producer. Once Base Polish implements
and publishes its own central assembly handoff, the same nearest-producer rule
will bind the following Review to Base Polish. The operator may remove, move, or
repeat Review or other stages in the editable occurrence list. An intent or
confirmed RunConfig containing T2T without a later Review remains valid;
recommendation does not become validation policy.

`selected_stage_occurrences` is the one editable draft sequence. Duplicate
stages remain distinct operator choices, each with its own local-process consent.
Method identity and parameters are not user-authored or interactively collected
draft fields. The TUI may edit only occurrence order, local-execution consent,
and confirmation. Confirmation compiles the draft through the same canonical
occurrence/freeze contract used by structured runs; missing required facts fail
closed at that boundary instead of becoming a parameter prompt. Runtime
recommendations that require upstream evidence remain deferred. If the planning
model fails or returns an invalid draft, the planning attempt stops and the
durable draft remains resumable; the TUI does not invent a deterministic
fallback stage plan.

The draft uses three distinct selection states:

```text
None  -> stage sequence unanswered; ask the stage-selection question
[]    -> explicit Inventory-only plan; ask neither consent, methods, nor parameters
[...] -> exact ordered biological occurrences; continue normal planning
```

Both plain-text and questionary stage prompts show an empty recommendation as
`Inventory only`; Enter accepts it. The LLM Plan Draft payload likewise requires
a JSON list rather than a non-empty list. An empty accepted list clears stale
method and parameter choices, renders `Inventory only` in the final summary,
and freezes as `requested_stages: []`.

## Whole-run freeze

Final confirmation invokes the same transaction-like publication boundary used
by the public structured `run` command:

```text
confirmed draft -> verified frozen transaction -> execute_frozen_session
```

Exact frozen files, publication order, hashes, idempotence, and crash behavior
belong to the [run-data contract](../reference/data-contract.md). A partial or
invalid freeze fails closed instead of silently rebuilding inputs.

After final confirmation and the TTY check, the TUI computes one canonical
effective local-method closure for fixed stages and captures the complete
configured candidate scope needed by deferred Assembly. A read-only readiness
pass probes the fixed closure plus any mandatory LLM endpoint; Assembly's
selected method is preflighted when its stage binds it. Only after the checks
needed for publication succeed does freeze store the dependency snapshot; no
session is created on readiness failure. Bootstrap remains the broader
machine-level diagnostic for all configured methods. After freeze, the executor
resolves from the snapshot rather than later edits to `config/envs.yaml`.

Whole-run freeze does not generate every future method command. Planning-time
shared-fact collection is derived from the declared deferred-Assembly scope:
a local Assembly occurrence requires a normalized `read_type` when available,
and requires either a manifest genome-size prior or a prior local Genome Survey;
otherwise planning and freeze validation fail closed. Each selected stage later
creates the narrower method-attempt freeze described in
[Method execution](execution.md).

The mutable draft also carries the two independent recommendation modes defined
by the [recommendation-policy contract](../reference/recommendation-policy.md).
They are planning choices, not method selections, and become immutable at the
same whole-run freeze boundary.

## Post-freeze convergence

The mutable TUI lifecycle ends at the whole-run freeze. Once the operator has
confirmed the Plan Draft, the TUI must not retain a TUI-specific executor,
review loop, countdown policy, or progress model. It calls the same
`execute_frozen_session` boundary as a structured run. The confirmed draft
must still have real stdin and stdout TTYs immediately before freeze; otherwise
it remains editable and no frozen contract is published.

A structured `gene-agent run` does not reconstruct its complete manifest and
RunConfig as a `TuiSessionDraft`. The supplied files are already the settled
draft: validation failure stops before freeze, while success freezes their
canonical meaning and immediately enters the shared interactive executor. This
preserves fields that the conversational intake model does not represent and
keeps Plan Draft editing solely on the TUI-authored path.
