# GapCloser and Base Polish Parent-Stage Registration Design

## Status

Approved for implementation on 2026-08-05.

## Goal

Register two canonical biological stages in the thin parent LangGraph without
claiming that either biological capability is implemented:

```text
hic_scaffolding -> gapcloser -> t2t -> base_polish -> repeat_annotation
```

`gapcloser` represents post-scaffolding structural gap filling. It is placed
after Hi-C scaffolding because it consumes a chromosome-scale scaffold and
before T2T finishing because telomere completion should inspect the structurally
repaired assembly.

`base_polish` represents base- and small-indel correction of the final assembly.
It is placed after T2T finishing so polishing can validate and correct the
adopted T2T product before repeat annotation consumes it.

## Scope

This change adds only parent-stage registration and an explicit fail-closed
placeholder boundary. It will:

- add `gapcloser` and `base_polish` to the canonical domain-stage order;
- add compiled stage subgraphs and parent-graph route targets;
- permit both names in `requested_stages`;
- keep both names out of `local_exec` because no executable method exists;
- preserve ordinary requested-stage skipping;
- stop with an explicit not-implemented stage status if either placeholder is
  requested;
- update routing/order tests and current architecture documentation.

## Non-goals

This change does not:

- register LR_Gapcloser, TGS-GapCloser, Racon, Medaka, NextPolish, Pilon, or
  another method;
- add descriptor, package, environment, bootstrap, command-freeze, runner, or
  collection configuration;
- define assembly artifact handoff beyond documenting that it is not yet
  implemented;
- add parameter recommendation, retry, review, or resume behavior;
- change the existing Assembly Review ability to adopt an externally polished
  FASTA;
- implement final integrated post-polish QA.

## Parent-graph design

`app.workflow.STAGE_ORDER` remains the sole canonical order. The new positions
are:

```text
06 hic_scaffolding
07 gapcloser
08 t2t
09 base_polish
10 repeat_annotation
11 gene_annotation
12 function_annotation
```

`app.graph.builder.build_graph` registers one compiled subgraph for each new
stage and includes both route names in the controller's conditional map and
return-to-controller edge list. `app.graph.routing.route_from_current_stage`
continues to derive the node name from the canonical stage name and needs no
stage-specific branch.

The public names are exactly `gapcloser` and `base_polish`.

## Placeholder behavior

Each placeholder subgraph has one bounded node. When entered because the stage
was explicitly requested, it writes a compact stage cursor:

```json
{
  "status": "blocked_not_implemented",
  "reason": "<stage> is registered in the parent graph but has no implemented method"
}
```

It then preserves that stage as `current_stage`, sets the top-level supervision
state to `failed_terminal`, and copies the reason into `last_error`. This is an
implementation boundary, not a biological failure and not a human-review gate;
using the existing terminal failure boundary avoids inventing a resumable gate
without a method, artifact, or decision contract.

Unrequested stages continue to be skipped by the parent controller according
to `requested_stages`. Therefore assembly/contig-focused runs that do not
request these stages retain their current behavior. A legacy run with
`requested_stages = null` must also retain its pre-change traversal semantics:
the placeholders are treated as unavailable optional capabilities rather than
silently becoming mandatory. The implementation must record an explicit skip
reason for this compatibility path and advance without claiming stage success.

## Configuration boundary

Both names are added to the requestable-stage schema so a frozen plan can state
the intent and receive the explicit not-implemented result. They are not added
to `LocalExecStage`, `RunConfig` execution blocks, descriptor `by_stage`, or
environment configuration. Consequently:

- requesting a placeholder is valid planning input;
- authorizing it in `local_exec` is invalid configuration;
- dependency snapshots do not invent method or environment requirements;
- the registry remains an inventory of implemented/selectable methods only.

## Data and resource behavior

Neither placeholder updates the central `assembly` resource. No artifact is
published and no path is copied into checkpoint state. Future executable stage
designs must separately define:

- accepted assembly and read resources;
- candidate and adopted assembly identities;
- frozen-attempt inputs and expected outputs;
- review/adoption semantics;
- resource provenance after commit.

## Documentation changes

Current architecture pages must distinguish graph wiring from biological
implementation:

- the parent Mermaid graph and canonical stage counts/order include both new
  nodes;
- the stage-contract reference adds numbered placeholder contracts that state
  their intended biological role and exact current failure boundary;
- the implementation map changes gap closing and automated base polishing from
  `unwired / absent` to `wired / placeholder`, while leaving registered methods
  and execution verification absent;
- flow descriptions in the README, review UI, and annotation-intake wording use
  the new canonical sequence where they describe downstream order;
- documentation must not imply that either stage produces an assembly today.

The historical design spec that recorded these capabilities as absent remains
historical and is not rewritten.

## Tests and acceptance

Focused tests must prove:

1. `STAGE_ORDER` contains both names in the approved positions;
2. the parent graph compiles with both route targets;
3. skipping from `hic_scaffolding` to a later requested stage records
   `gapcloser`, `t2t`, and/or `base_polish` according to canonical precedence;
4. a contig/assembly-only requested-stage plan never enters either placeholder;
5. explicitly requesting either placeholder reaches its subgraph and terminates
   with `blocked_not_implemented` evidence rather than successful completion;
6. `requested_stages` accepts both names while `local_exec` rejects them;
7. the relevant existing routing, run-config, state-schema, planner, and project
   scaffold tests still pass.

No test is considered passing until its process exits successfully and its
expected assertions execute.

## Compatibility and risk

The main compatibility risk is accidentally changing `requested_stages = null`
from the historical full-pipeline mode into an unconditional failure at the
first placeholder. The explicit unavailable-capability skip described above is
required to prevent that regression.

The second risk is documentation overclaim. The implementation map must use
`placeholder`, not `implemented-no-process`, because there is no meaningful
biological operation, completed artifact, or resource commit.

The change is reversible by removing the two stage packages, their canonical
names and route registrations, and their documentation/test updates. It adds no
production dependency or persistent-data migration.
