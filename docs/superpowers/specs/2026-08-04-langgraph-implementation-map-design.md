# LangGraph Implementation Map Documentation Design

**Date:** 2026-08-04
**Status:** approved in conversation; ready for specification review

## Objective

Add one durable, developer-facing LangGraph implementation map to the current
architecture documentation. The map must let a developer entering through
`docs/README.md` quickly distinguish:

- the thin parent graph from compiled stage subgraphs;
- graph wiring from real executable behavior;
- registered methods from unregistered future capabilities;
- parameter catalogs, recommendation, approval, and attempt freezing;
- verified implementation from partial implementation, placeholders, and
  capabilities that have not been modeled at all.

The internal map is committed and maintained as current architecture evidence.
The separate external presentation mind map is not committed and has no
repository maintenance contract; its complete copyable node list is delivered
to the user after the internal documentation change.

## Scope

Create:

- `docs/architecture/topics/langgraph-implementation-map.md`

Update:

- `docs/README.md` so a new developer can find the implementation map directly
  and understand when to use it;
- `docs/architecture/README.md` so the topic router includes the map and its
  maintenance rule;
- the documentation status rule in `docs/README.md` so it distinguishes a
  prohibited historical status snapshot from this evidence-backed current
  implementation map.

Do not change production code, stage routing, descriptors, tests, or method
behavior. Do not add a generator, new schema, dependency, progress database,
or external-presentation artifact.

## Two-view boundary

The repository owns only the internal view.

| View | Audience | Contents | Repository status |
|---|---|---|---|
| Internal LangGraph implementation map | Developers and reviewers | Parent routing, stage nodes, method adapters, parameter flows, gates, resource commits, verification, and known debt | Committed and maintained |
| External biological workflow mind map | Presentation audience | Assembly delivery chain, inputs/outputs, representative methods, capability maturity, and downstream annotation | Delivered as copyable text only; not committed |

The external list may summarize repository facts at delivery time, but it is
not a second source of truth and does not create an update obligation.

## Internal document structure

The implementation map is a topic page, not a new reference-contract owner. It
must use the same topic metadata as the existing architecture pages: status,
reading trigger, incoming and outgoing edges, contract dependencies, and code
evidence.

The page contains five sections.

### 1. Reading contract and status axes

Every classified unit uses four independent axes. A stage cannot be called
complete merely because its builder compiles.

| Axis | Values | Meaning |
|---|---|---|
| Wiring | `wired`, `unwired`, `not-applicable` | Whether the unit is reachable in the compiled graph or registered route |
| Implementation | `executable`, `implemented-no-process`, `partial`, `placeholder`, `absent` | Whether the unit forms a real execution or deliberately non-process behavior |
| Verification | `tested`, `partially-tested`, `untested` | Whether focused tests exercise the claimed boundary |
| Debt | `none`, `known-gap`, `contract-drift` | Whether an explicit limitation or conformance mismatch remains |

`not required` is recorded as a capability note, not treated as a failure or
placeholder. For example, not every stage needs an LLM parameter recommender.

Classification is evidence-based:

- `wired` requires a builder edge, conditional route, parent registration, or
  registered method route;
- `executable` requires a real method adapter and shared execution lifecycle,
  not only a descriptor or planned cursor;
- `implemented-no-process` is reserved for intentional paths such as a
  registered skip method;
- `placeholder` requires an existing named interface, descriptor, or graph
  position that does not form an executable closed loop;
- `absent` means the capability has no canonical stage, registered method, or
  graph contract;
- verification claims name focused test files;
- conformance claims link to `reference/conformance.md` rather than copying its
  full prose.

### 2. Parent graph drawing

Include a Mermaid flowchart of the actual parent topology:

```text
START -> route_controller -> selected compiled stage -> route_controller -> END
```

The drawing must show the canonical 11-stage ordering from `app/workflow.py`,
the fact that unrequested stages may be skipped, and the terminal boundaries
for completion, failure, and human blocking. It must not misrepresent the
parent as a simple unconditional linear pipeline.

### 3. Stage and method implementation inventory

For every canonical stage, list the actual graph-node groups and registered
methods. The readable unit is a capability group, with exact node names where
those names matter:

- input resolution and validation;
- selection or planning;
- parameter catalog/recommendation/approval when present;
- executable method subgraph or deliberate no-process method;
- output collection and validation;
- human review gate when present;
- resource commit, failure, and terminal behavior.

Each registered method entry must state whether a descriptor, command adapter,
shared lifecycle integration, output collection, and focused tests exist. The
map must cover all method IDs in `descriptors/registry.yaml`, not only the
assembly methods.

The visual drawing may group detail to remain readable, but the accompanying
tables must preserve the node- and method-level status. The page must not use a
single green/yellow/red label for an entire stage.

### 4. Parameter capability matrix

Separate these concepts instead of using the vague phrase "parameter support":

- static parameter catalog;
- automatic or LLM recommendation;
- immutable recommendation evidence;
- human approval gate;
- approved-value binding into a frozen execution attempt;
- retry behavior.

Record Assembly, AutoHiC, and T2T as the current formal recommendation flows,
subject to their actual method coverage and known gaps. Other stages must be
classified as `manual/static configuration`, `not required`, or
`not implemented`; they must not be collectively labeled as stale parameter
recommendations.

### 5. Explicit missing-capability boundary

Show these product-level capabilities separately from registered stages:

- gap closing: `absent` unless and until a canonical stage and executable
  method are registered;
- base polishing: `absent` as an automated stage; the Assembly Review ability
  to adopt an operator-provided polished FASTA is not a polishing executor;
- final integrated QA: `absent` as one terminal stage; current QUAST/BUSCO
  Assembly Review and stage-local gates are implemented evidence but do not
  constitute a comprehensive post-polish final QA workflow.

This section prevents roadmap concepts from being mistaken for current
execution support.

## Documentation navigation and governance

`docs/README.md` must add the implementation map to the initial reading path.
The intended fast path is:

1. documentation map;
2. system map for conceptual ownership;
3. LangGraph implementation map for current wiring and capability maturity;
4. exact reference contract only when needed.

`docs/architecture/README.md` must add a topic-router question equivalent to
"Which nodes and methods are actually implemented?" and point it to the new
page.

The existing rule against status snapshots must be narrowed, not deleted. It
continues to forbid phase reports, historical progress logs, and duplicate
architecture summaries. It explicitly permits this one current implementation
map because every claim is tied to code, registry, tests, or conformance
evidence and because it does not own or weaken exact contracts.

The maintenance rule is:

- any change that adds, removes, or rewires a parent node, stage node,
  registered method, method adapter, parameter-recommendation boundary, review
  gate, or cross-stage resource commit must update the implementation map in
  the same change;
- closed gaps are removed from both the conformance page and the map reference
  in the same change;
- historical states remain in Git history and are not accumulated in the map.

## Accuracy and review procedure

Before writing the status inventory, verify claims against:

- `app/workflow.py` and `app/graph/` for parent routing;
- every `app/stages/*/builder.py` for node wiring;
- `descriptors/registry.yaml` for the complete registered-method set;
- `app/stages/*/method_application/` and `app/execution/` for executable
  adapters and lifecycle integration;
- focused tests under `tests/` for verification labels;
- `docs/architecture/reference/conformance.md` for known contract drift.

Because this is documentation-only, verification consists of:

1. checking all local Markdown links added or changed;
2. comparing documented stages with `STAGE_ORDER`;
3. comparing documented method IDs with `descriptors/registry.yaml`;
4. confirming every `tested` label names at least one focused test file;
5. running `git diff --check` on the edited documentation.

No production test-suite pass is claimed by this documentation change.

## Acceptance criteria

The change is accepted when:

- a developer starting at `docs/README.md` can reach the internal map in one
  click;
- the parent Mermaid drawing represents conditional stage selection and all
  terminal boundaries accurately;
- all 11 canonical stages and all registered methods are represented;
- node/method status uses the four independent axes rather than one stage-level
  completion color;
- Assembly, AutoHiC, and T2T parameter flows are distinguished from manual,
  unnecessary, and missing recommenders;
- gap closing, automated base polishing, and final integrated QA are visibly
  absent rather than presented as implemented;
- every status claim has code, registry, test, or conformance evidence;
- the external presentation hierarchy is delivered to the user but no
  external-presentation file is committed.

## Change safety

The worktree already contains user-owned documentation reorganization and
other unrelated edits. Preserve them. Modify only the new implementation map
and the directly affected navigation/governance passages in the two current
README files. Do not restore deleted legacy documentation paths or stage
unrelated changes.
