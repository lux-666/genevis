# Review Producer Binding And Planning Hardening Design

## Status

Approved by the user on 2026-08-06. This specification makes Assembly Review
consume an exact producer occurrence, enforces the mandatory review boundaries
for Assembly and T2T, completes the natural Inventory-only planner path, and
closes two strictness/documentation gaps.

## Problem

The frozen workflow preserves repeated stage occurrences, but an
`assembly_review` occurrence currently resolves only the central `assembly`
resource that happens to be current at runtime. The frozen step does not name
the producer occurrence that it must review. Consequently, sequence validity
and review correctness are inferred from mutable current state rather than
proved by the frozen plan.

This has four concrete effects:

- a file-authored RunConfig can request `assembly` or `t2t` without a later
  `assembly_review`;
- repeated Review occurrences cannot prove which exact producer occurrence
  supplied their subject;
- a Review may silently consume a different current assembly if the handoff
  drifts before it starts;
- Review reuse is easily described incorrectly as "the first Review reviews
  Assembly and the second reviews T2T", even though occurrence order, not
  ordinal position, must define the dependency.

Three adjacent conformance gaps are also in scope:

- when intent and manifest-based recommendations are both empty, the planner
  still recommends the first two available stages instead of Inventory only;
- `ResourceRef.from_dict()` coerces non-string provenance fields before the
  strict constructor sees them;
- the resource-handoff documentation incorrectly treats Inventory-wrapped
  registered assemblies as ordinary provenance-free external resources.

## Goals

- Bind every Assembly Review occurrence to one exact upstream producer step.
- Derive that binding from the literal frozen occurrence sequence, never from
  "first Review" or "second Review" rules.
- Reject a RunConfig that leaves a required Assembly or T2T review boundary
  unsatisfied.
- Let T2T hand off its selected final assembly to Review even when no patch was
  adopted and the bytes are unchanged.
- Keep QUAST/BUSCO and other Assembly Review settings stage-scoped for now.
- Make an empty planner recommendation naturally mean Inventory only.
- Reject non-string `ResourceRef` provenance before coercion.
- Align external-assembly provenance documentation with Inventory's executed
  candidate implementation.

## Non-Goals

- No runtime insertion, deletion, or reordering of workflow occurrences.
- No ordinal mapping such as "Review #1 -> Assembly" or "Review #2 -> T2T".
- No occurrence-local QUAST/BUSCO configuration in this change.
- No general DAG planner or arbitrary resource-expression language.
- No change to runtime-orchestration or repeated-occurrence documentation that
  depends on the broader occurrence-local configuration question; the finding
  grouped as documentation item 6 remains deferred.
- No conditional skipping of post-T2T Review when T2T keeps the prior bytes.

## Considered Approaches

### 1. Frozen producer-occurrence binding — selected

Extend the immutable workflow-step projection so an `assembly_review` step
contains a bounded target naming the logical resource and exact producer step:

```json
{
  "step_id": "step-008-assembly_review",
  "stage": "assembly_review",
  "allow_local_execution": true,
  "review_target": {
    "resource_type": "assembly",
    "producer_step_id": "step-007-t2t"
  }
}
```

The projection derives the target from the nearest preceding assembly handoff
occurrence. Runtime Review then verifies that the central assembly resource was
handed off by the bound producer step before loading its candidate bundle.

This makes the frozen plan the dependency authority, distinguishes repeated
same-stage producers, and reuses the existing `ResourceRef.workflow_step_id`
provenance.

### 2. Mutable `pending_review_target` state

Each producer could write a top-level marker for the router or Review stage.
This duplicates `ResourceRef` provenance, creates a second mutable authority,
and makes checkpoint reconciliation harder. It is rejected.

### 3. Stage-name-only assertion

Review could assert only `source_stage == "t2t"` or another expected stage.
That cannot distinguish repeated T2T occurrences and therefore does not close
the occurrence-integrity gap. It is rejected.

## Frozen Workflow Contract

### Review target type

`WorkflowStep` gains an optional `review_target` field. It is required exactly
when `stage == "assembly_review"` and forbidden for all other stages. The target
contains only:

- `resource_type`, currently fixed to `assembly`;
- `producer_step_id`, a deterministic step ID already present in the same
  frozen workflow list.

`validate_workflow_steps()` verifies:

- the target object has no unknown or missing fields;
- `producer_step_id` refers to an earlier workflow step;
- the referenced step is an allowed assembly handoff producer;
- the target is the producer selected by the shared binding algorithm, so a
  checkpoint cannot substitute another earlier producer while retaining valid
  syntax.

The implicit Inventory occurrence is eligible as the initial producer target.
This preserves the supported external-assembly flow
`requested_stages=[assembly_review]`: Inventory first wraps the registered
assembly as an executed candidate and Review binds to `step-000-inventory`.
If Inventory does not actually provide an assembly, normal resource resolution
fails closed at runtime.

### Binding algorithm

The binding algorithm scans the exact occurrence list from left to right and
tracks the latest assembly handoff occurrence. Assembly-producing/refining
stages replace that producer pointer. When it reaches `assembly_review`, it
binds the Review to the current pointer and then treats the completed Review as
the latest handoff for any later explicit Review.

The initial pointer is `step-000-inventory`. The concrete handoff-producer set
is centralized and initially includes stages whose successful commit owns the
central assembly delivery contract:

- `inventory`;
- `assembly`;
- `assembly_dedup`;
- `assembly_review`;
- `hic_scaffolding`;
- `t2t`.

A stage is not added merely because it may eventually manipulate assemblies;
it must implement the central ResourceRef handoff contract first.

Producer placement, not Review ordinal position, selects the target. For
example:

```text
assembly -> assembly_dedup -> assembly_review
```

binds Review to the Assembly Dedup occurrence, while:

```text
assembly_review -> hic_scaffolding -> t2t -> assembly_review
```

binds the later Review to the T2T occurrence.

## Mandatory Review Validation

RunConfig validation and workflow projection share one pure sequence-analysis
helper so schema acceptance and frozen binding cannot drift.

The validator enforces two business boundaries:

1. An `assembly` occurrence opens a required pre-finishing review boundary.
   Assembly Dedup and other assembly refinements may update the subject of that
   boundary, but a matching `assembly_review` must close it before T2T begins or
   the workflow ends.
2. Every `t2t` occurrence opens a new required post-T2T review boundary. A
   matching `assembly_review` bound to that T2T handoff must close it before
   another T2T occurrence or workflow termination.

This preserves the normal template:

```text
assembly -> assembly_dedup -> assembly_review
         -> hic_scaffolding -> t2t -> assembly_review
```

It rejects, among other invalid sequences:

```text
assembly
assembly -> t2t -> assembly_review
t2t
t2t -> t2t -> assembly_review
assembly -> assembly_review -> t2t
```

The last sequence is invalid only because the T2T boundary remains open at
termination. The validator reports the exact producer step and missing Review
boundary. It never inserts the missing occurrence.

A standalone `assembly_review` remains schema-valid and binds to Inventory.
A standalone `t2t -> assembly_review` also remains valid for a registered
external assembly.

## Runtime Review Resolution

Assembly Review continues to consume the canonical central assembly resource
and verified executed candidate bundle. It does not gain producer-specific
fallback branches.

Before candidate loading, `resolve_candidates()` obtains the current workflow
step and its frozen `review_target`, then verifies:

```text
current assembly ResourceRef.workflow_step_id
    == review_target.producer_step_id
```

It also verifies that the referenced producer step's stage agrees with the
ResourceRef's canonical `source_stage`. A mismatch is a bounded stage failure
before QUAST/BUSCO attempts, artifacts, or review requests are created.

The existing candidate-bundle and primary-output equality checks remain the
content authority. Producer binding supplements them; it does not replace
them.

## Unchanged T2T Output

The business meaning of the Review binding is "the producer occurrence's final
delivery", not "new bytes created by that occurrence". Therefore a T2T
occurrence that keeps its input assembly still owns the selected final handoff
for review purposes.

`commit_t2t()` must republish the central ResourceRef with
`source_stage="t2t"` and the active T2T `workflow_step_id` while preserving the
verified candidate bundle when no patch is adopted. The current
`current_assembly_matches_output` early-return path does not perform that
handoff when output and input are the same assembly; it must be changed to
distinguish an already occurrence-bound idempotent replay from an unchanged
assembly still attributed to an earlier producer. The former may return
unchanged after verification. The latter republishes the existing verified
candidate metadata under the active T2T occurrence before advancing. The
post-T2T Review then runs normally against the same candidate content with the
new occurrence-level delivery provenance.

Every other producer in the centralized set must follow the same delivery
rule: successful completion leaves the central assembly ResourceRef attributed
to that occurrence, even when the selected bytes are unchanged. In particular,
the Hi-C `keep_prior_assembly` completion path is brought into line with T2T by
republishing the verified prior candidate as an unchanged Hi-C delivery
handoff. Stop and retry paths do not commit a handoff. Focused tests prevent a
frozen target from pointing at a producer that cannot satisfy this runtime
contract.

## Stage Seed Scope

`stage_seeds["assembly_review"]` remains a stage-scoped immutable seed.
Repeated Review occurrences each receive a fresh copy of that same seed when
entered. This change does not add configuration A/configuration B semantics.

Occurrence locality in this design applies to:

- frozen Review target;
- workflow step ID;
- execution consent;
- attempts, artifacts, review requests, decisions, and Ledger facts.

Method configuration remains shared across occurrences until a separate
configuration design is approved.

## Inventory-Only Planner Default

`_suggested_stages()` keeps the existing precedence:

1. exact explicit intent occurrences;
2. manifest-derived recommendation;
3. otherwise an empty list.

The final fallback changes from `available[:2]` to `[]`. This means a missing,
empty, or non-recommending manifest naturally presents `Inventory only`, which
the already-correct Ask/TUI response path can accept by pressing Enter.

The available-stage catalog remains unchanged and is still displayed for
manual editing. Only the recommendation changes.

## ResourceRef Strict Deserialization

`ResourceRef.from_dict()` validates original values before construction:

- `resource_type` must be a string;
- `source_stage` must be either `None` or a string;
- `workflow_step_id` must be either `None` or a string;
- `paths` remains a list whose members are validated by the constructor;
- `metadata` remains a dictionary.

No `str()` call is permitted before these checks. Numeric, boolean, list, or
object provenance values fail closed instead of becoming strings.

## External Assembly Provenance Documentation

The resource-handoff reference distinguishes two cases:

- ordinary manifest/external resources retain `source_stage=None` and
  `workflow_step_id=None`;
- a registered assembly is converted by Inventory into an immutable executed
  assembly candidate, so the resulting central `assembly` ResourceRef is a
  stage-owned handoff with `source_stage="inventory"` and
  `workflow_step_id="step-000-inventory"`.

The latter is an intentional provenance upgrade, not an exception to the
paired-provenance invariant.

## Error Handling

- Missing required Review boundaries fail RunConfig validation before freeze.
- Malformed or substituted frozen Review targets fail workflow-step validation.
- Runtime producer drift fails Assembly Review before method execution or human
  review creation.
- Missing Inventory assembly for an Inventory-bound Review fails normal
  resource resolution.
- Non-string ResourceRef provenance fails deserialization without coercion.
- Planner absence of a recommendation is represented by `[]`, not by an
  arbitrary fallback stage prefix.

## Verification

Focused tests must prove:

- `assembly` without a closing Review is rejected;
- `assembly -> assembly_dedup -> assembly_review` is accepted and Review binds
  to the exact Dedup step;
- `assembly -> t2t -> assembly_review` is rejected because Assembly lacked its
  pre-T2T Review;
- every T2T occurrence requires its own later Review;
- the recommended two-Review T2T template validates;
- `assembly_review` alone binds to `step-000-inventory`;
- repeated same-stage producers produce distinct Review target step IDs;
- a checkpoint with a forged or stale Review target is rejected;
- Review rejects a central assembly produced by another occurrence before any
  evaluation attempt;
- a no-patch T2T handoff is still accepted by its bound post-T2T Review;
- the `current_assembly_matches_output` T2T path republishes an unchanged
  earlier-producer assembly under the active T2T step, while an already
  T2T-bound replay remains idempotent;
- a Hi-C keep-prior completion publishes an unchanged occurrence-owned handoff
  that a following Review can verify;
- no intent plus no manifest recommendation yields an empty suggested list and
  the ordinary Inventory-only confirmation path;
- numeric `source_stage` or `workflow_step_id` values fail
  `ResourceRef.from_dict()`;
- valid string/`None` ResourceRefs retain current behavior;
- resource-handoff documentation names the Inventory registered-assembly
  provenance upgrade.

Run the focused unit/integration tests, formatting, lint, typing, and complete
test suite. A command counts as passing only after it exits zero with the
expected assertions.

## Acceptance Criteria

1. Every mandatory Assembly/T2T review boundary is enforced before freeze.
2. Every Assembly Review occurrence names and consumes one exact producer
   occurrence without ordinal assumptions.
3. T2T keep-prior delivery remains reviewable.
4. Planner recommendations can naturally be Inventory only.
5. ResourceRef deserialization is strictly typed before conversion.
6. External assembly provenance documentation matches the Inventory candidate
   implementation.
7. Documentation finding 6 remains untouched pending the separate
   occurrence-local configuration decision.
