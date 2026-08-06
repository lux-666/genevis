# Reusable Assembly Review After Derived Assemblies Design

## Status

Approved by the user on 2026-08-06. This specification preserves explicit
frozen occurrence routing while making the existing Assembly Review component
reusable after an adopted T2T or Hi-C assembly revision.

## Problem

The parent graph already supports repeated stage occurrences, but the biological
handoff contract prevents the intended reuse:

- Assembly Review requires the current central assembly resource to carry a
  verified executed `candidate_bundle_ref` whose primary output exactly matches
  the resource path.
- Assembly and Assembly Dedup publish that contract.
- Assembly Review action outputs publish a derived candidate contract.
- adopted Hi-C and T2T outputs replace the assembly resource with a new
  `primary_output_ref` but retain only `input_candidate_bundle_ref`; they do not
  publish a candidate bundle for the new assembly.

Consequently an explicit `t2t -> assembly_review` occurrence sequence fails as
soon as T2T adopts a changed assembly. Hi-C has the same latent limitation.

The top-level README also presents a fixed arrow chain, which obscures the real
model: the parent graph is static, while a frozen occurrence list supplies the
run-specific order.

## Goals

- Keep the frozen occurrence list as the only runtime successor authority.
- Make every adopted assembly revision a canonical executed assembly candidate.
- Let Assembly Review consume the current assembly after Assembly, Dedup, Hi-C,
  T2T, or a prior Review action through one unchanged candidate contract.
- Recommend an explicit post-T2T Assembly Review occurrence for T2T goals.
- Preserve repeated-occurrence identity, checkpoint reset, artifact namespacing,
  review binding, and occurrence-local execution consent.
- Document accurately that stage subgraphs are composable but currently
  statically registered rather than dynamically plug-in loaded.

## Non-Goals

- No runtime auto-insertion of Assembly Review.
- No stage-owned successor hooks.
- No rewrite of an operator's frozen occurrence sequence.
- No generalized plugin discovery or entry-point registry in this change.
- No new final integrated QA stage or new biological evaluation method.
- No automatic acceptance of Review decisions.
- No change to the fact that methods and parameters remain stage-scoped while
  local execution consent is occurrence-scoped.

## Routing Model

The existing parent model remains authoritative:

```text
RunConfig.requested_stages
  -> immutable workflow_steps
  -> workflow_step_index
  -> route_controller
  -> registered stage subgraph
  -> advance_workflow_step
  -> route_controller
```

There are no biological stage-to-stage parent edges. `STAGE_ORDER` remains a
unique registry/display ordering and must not become a successor table. A stage
commit advances exactly one frozen occurrence and cannot append an implicit
Review step.

The intended default T2T workflow is represented explicitly, for example:

```text
read_qc
genome_survey
assembly
assembly_dedup
assembly_review
hic_scaffolding
t2t
assembly_review
```

Inventory remains implicit. The operator sees and confirms both Review
occurrences and grants local process execution independently for each one.

## Default Recommendation Versus Registry Order

A unique `STAGE_ORDER` cannot represent a repeated post-T2T Review occurrence.
Default planning therefore needs a workflow-template helper distinct from the
registry list. The T2T goal template explicitly appends `assembly_review` after
`t2t`; other endpoint templates retain their intended occurrence sequence.

The existing registry/default-prefix helper may remain for simple unique
prefixes, but TUI intent and planner recommendation code use the occurrence
template when constructing an end-to-end T2T draft. Public structured RunConfig
continues to accept any schema-valid literal order and is never silently
expanded.

## Canonical Derived Candidate Contract

Every accepted change to the central assembly resource must publish or preserve
an executed `AssemblyCandidateBundle` whose primary output is exactly the new
resource path.

The resource metadata invariant after an assembly-changing commit is:

```json
{
  "candidate_bundle_ref": {"artifact_type": "..."},
  "primary_output_ref": {"artifact_type": "..."},
  "input_candidate_bundle_ref": {"artifact_type": "..."},
  "input_primary_output_ref": {"artifact_type": "..."}
}
```

The `input_*` fields are lineage projections and may be omitted only when the
source is an external registered assembly with no prior derived lineage beyond
its Inventory candidate. `candidate_bundle_ref` and `primary_output_ref` are the
current authority and are mandatory for any stage-produced assembly.

The existing `publish_derived_assembly_candidate` boundary is reused. It binds:

- the source verified candidate bundle;
- the new primary output `ArtifactRef`;
- the producing stage and workflow occurrence;
- the producing method/action identity;
- dependency refs that preserve lineage.

No stage fabricates a candidate DTO directly or copies the prior candidate ref
while changing the primary path.

The candidate loader and derived publisher currently accept only
`assembly.draft_assembly` as a primary output type. This change deliberately
expands the canonical candidate-primary type set to exactly:

- `assembly.draft_assembly`;
- `hic_scaffolding.chromosome_scaffolded_assembly`;
- `t2t.patched_assembly`.

Both publication and loading validate against the same central type set. The
bundle artifact type remains `draft_assembly.candidate` for this change; it is
the existing compatibility identity of the generic Assembly candidate manifest,
not a requirement to duplicate every derived FASTA under a draft-assembly
artifact. Hi-C and T2T therefore retain their biologically specific primary
artifact refs and do not republish or copy the same bytes merely to satisfy the
candidate wrapper.

## T2T Commit

When T2T keeps the prior assembly unchanged, it preserves the existing central
resource and candidate metadata.

When an adopted patch changes the assembly:

1. resolve and verify the current assembly resource;
2. load its current executed candidate bundle;
3. verify the patched assembly artifact and exact output path;
4. publish a derived executed candidate in the active T2T occurrence;
5. replace the central assembly ResourceRef with the new path,
   `candidate_bundle_ref`, and matching `primary_output_ref`;
6. retain the prior candidate and primary-output refs as lineage metadata;
7. advance the frozen workflow cursor.

Candidate publication occurs only after the existing T2T adoption decision has
authorized the output. A rejected patch leaves the prior resource unchanged.
The derived candidate `action_identity` is the immutable artifact ID of the
persisted T2T adoption decision. An adopted output without that verified
decision identity fails closed, ensuring replay derives the same candidate ID
and same-occurrence conflicts remain detectable.

## Hi-C Adoption

Accepted AutoHiC scaffolding follows the same contract:

1. verify the pre-Hi-C assembly and its candidate bundle;
2. verify the adopted scaffold FASTA artifact;
3. publish a derived executed candidate in the active Hi-C occurrence;
4. update the central resource with current candidate and primary-output refs;
5. preserve prior refs as lineage metadata.

`keep_prior_assembly` preserves the current resource unchanged. Parameter retry
and stop decisions do not publish a derived candidate.
The derived candidate `action_identity` is the immutable artifact ID of the
persisted Hi-C adoption decision. Accepted scaffolding without that verified
decision identity fails before candidate publication or resource replacement.

## Assembly Review Reuse

Assembly Review retains one input contract: current assembly resource plus
current executed candidate bundle. It does not add T2T-specific or Hi-C-specific
fallback branches.

On repeated entry, `seed_current_workflow_step` replaces the stage's scratch
cursor from immutable `stage_seeds`. Prior Review occurrence state is not reused.
Historical records remain distinct because:

- artifacts and work directories are step-scoped;
- Review requests and decisions carry workflow step IDs;
- method attempts are occurrence-namespaced;
- Ledger stage and review facts are occurrence-bound;
- resource lineage points to the exact producer candidate.

A post-T2T Review may run the same configured QUAST/BUSCO component and review
rules. This change does not claim that those methods form a new final integrated
QA product.

## Static Registration Boundary

The current system is composable but not dynamically plug-in registered.
`app/graph/builder.py` imports and maps each stage explicitly, while
`app/workflow.py` provides registration/display/default-planning data. This
change documents that fact and does not opportunistically introduce a new
plugin framework.

A future plugin-registry refactor should centralize stage name, builder,
availability, and default-planning metadata, but it must preserve frozen
occurrence routing and is outside this implementation plan.

## Error Handling

- A changed assembly without a verified source candidate fails before resource
  replacement.
- A new primary output whose ArtifactRef does not match the adopted path fails
  before candidate publication.
- A derived candidate whose published primary output drifts from the supplied
  artifact fails through the existing candidate publication boundary.
- A candidate primary artifact outside the exact Assembly, Hi-C-scaffolded, and
  T2T-patched type set fails both publication and loading.
- An already-published identical derived candidate is recovered idempotently;
  conflicting content at the same occurrence identity fails closed.
- Assembly Review continues to reject a resource/path/candidate mismatch rather
  than falling back to historical input metadata.
- No planning helper modifies a file-authored frozen occurrence sequence.

## Documentation Changes

Update:

- top-level README, replacing the fixed runtime arrow-chain label with stage
  registry/default recommendation language and an explicit occurrence example;
- `topics/runtime-orchestration.md`, preserving explicit sequence authority and
  documenting repeated Review occurrences;
- `reference/resource-handoff.md`, documenting derived candidate lineage for
  all assembly-producing stages;
- `reference/stage-contracts.md`, clarifying Assembly Review's reusable current
  candidate input contract;
- `langgraph-implementation-map.md`, adding evidence for post-T2T and post-Hi-C
  Review reuse and avoiding a false “final QA” claim;
- `reference/conformance.md`, recording the current handoff gap until focused
  tests prove closure.

## Verification

Focused tests must prove:

- a T2T goal recommendation contains two distinct Assembly Review occurrences
  in the intended order;
- structured RunConfig order is not automatically expanded or rewritten;
- T2T adopted patch publication creates a derived candidate bundle whose primary
  output matches the central resource;
- candidate publication and loading accept the three canonical primary assembly
  artifact types and reject unrelated artifact types;
- adopted T2T and Hi-C candidates derive deterministic identity from the exact
  persisted adoption-decision artifact;
- T2T keep-prior behavior preserves the existing candidate resource unchanged;
- accepted Hi-C scaffolding creates the same canonical derived candidate handoff;
- Hi-C keep-prior behavior publishes no new candidate;
- `t2t -> assembly_review` resolves candidates and reaches its normal review
  workflow after a changed patch;
- `hic_scaffolding -> assembly_review` does the same after accepted scaffolding;
- repeated Assembly Review occurrences have distinct step IDs, artifacts,
  attempts, review requests, decisions, and Ledger facts;
- Review still rejects mismatched resource paths and candidate refs;
- README and architecture text do not describe `STAGE_ORDER` as runtime
  successor order or claim a dynamic plugin registry.

After focused tests, run formatting, lint, typing, and the complete test suite.

## Acceptance Criteria

1. The frozen occurrence list remains the only runtime routing authority.
2. Every adopted T2T or Hi-C assembly has a canonical current candidate bundle.
3. Assembly Review can run after those derived assemblies without producer-
   specific fallback logic.
4. Default T2T planning explicitly exposes and confirms the post-T2T Review
   occurrence.
5. Documentation distinguishes composable stage subgraphs, static registration,
   default recommendations, and runtime occurrence order.
