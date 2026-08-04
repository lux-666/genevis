# Registered Implementation Drift Closure Design

**Date:** 2026-08-03
**Status:** approved for specification by the user; implementation requires the
normal planning and review gates.

## Objective

Close the six registered implementation drifts named in the request as one
contract-first change set. Every production-code change must update focused
tests and its owning architecture document in the same change. Once a listed
conformance gap is objectively closed, delete that gap from
`docs/architecture/reference/conformance.md` instead of retaining historical
debt text.

The implementation order is:

1. converge the behavior contract;
2. update focused tests to enforce the accepted contract;
3. update the unique owning documents;
4. delete the closed conformance entries;
5. run focused tests, then mypy and ruff for same-source defects, then the full
   repository quality gates.

## Baseline and scope boundary

The read-only baseline command completed with exit 1:

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n gene_agent \
  python -m pytest -p no:cacheprovider -ra
```

Result: `113 failed, 1401 passed, 1 skipped` in 75.29 seconds.

The 113 failures group into ten root causes:

| Root cause | Failures |
|---|---:|
| strict input-integrity and registry provenance fixtures | 55 |
| Assembly Review candidate ArtifactRef/cursor migration | 33 |
| removed `ResumeReviewRequest.final_assembly_ref` | 9 |
| planner-only/local-executable stage classification | 6 |
| TUI executable-method planning check | 1 |
| Flye planned-candidate shape | 1 |
| missing `tests/hpc/common.sh` | 4 |
| removed resource-resolution `override=` argument | 1 |
| removed legacy CLI `run` command | 1 |
| removed Assembly cursor `selected_methods` field | 1 |

The six registered drifts directly explain only about eight current failures.
Genome Survey and AutoHiC drift tests currently pass because they still encode
the old behavior or because an earlier input-integrity failure masks the later
boundary. Therefore this design does not promise that closing the six listed
gaps makes the full suite green.

The other failure clusters are explicitly out of scope. They require separate
contract decisions and must not be hidden behind opportunistic compatibility
branches. The repository-quality conformance entry remains until every declared
quality-gate command actually returns zero.

Before implementation edits begin, rerun the baseline pytest command and all
four declared repository gates against the same working tree. Retain the
complete failing pytest node-ID set, full ruff/mypy output, formatter result,
`git rev-parse HEAD`, and `git status --short`. Counts and root-cause labels are
diagnostic summaries, not regression evidence. Closure of this design requires
that every focused drift test pass and that the post-change full-suite failure
set introduce no node ID absent from the retained baseline.

For ruff and mypy, compare a normalized diagnostic multiset keyed by tool,
repository-relative path, rule/error code, and normalized message; retain line
and column for reporting but exclude them from identity because edited lines can
move. For `ruff format --check`, retain the reported file set. Every edited file
under `app/` must pass its applicable focused format, ruff, and mypy check;
edited Python tests must pass applicable format/ruff checks. YAML validity is
proved through complete registry loading, and Markdown is reviewed as
documentation rather than passed to mypy. The full commands must introduce no
new diagnostic identity or newly unformatted Python file. Configuration must
not be weakened to make either comparison pass.

The six closure units below are independently acceptable. Each unit owns one
behavior boundary, its focused tests, and its architecture claims. A unit must
not depend on another drift fix merely to make its tests pass. Only four units
close registered conformance entries; the cleanup section maps those entries
explicitly. Edits to shared files remain serialized, but shared file ownership
does not merge the behavioral contracts.

## Accepted changes

### 1. Canonical Genome Survey input family

The externally retained Genome Survey input family is exactly
`short_reads | long_reads`.

- `app/stages/genome_survey/nodes.py` resolves both logical resources through
  the canonical resource resolver and consumes the resolver's selected paths
  without re-ranking them by clean/raw asset type.
- `state.resource_refs[logical_resource]` remains authoritative. Therefore a
  Read QC-produced `long_reads` handoff wins automatically when Read QC ran;
  otherwise the resolver uses the current direct resource or its asset-registry
  fallback. Genome Survey does not decide whether a path is raw or clean.
- When both logical families are available, `short_reads` remains the accepted
  survey family; this is a logical-resource decision, not a clean/raw lifecycle
  policy.
- The checkpoint cursor, result metadata, and tests retain the selected logical
  family and exact input references. They do not expose clean/raw asset types as
  a second Genome Survey input contract.
- If usable reads exist, the survey runs even when an explicit genome size is
  present; the explicit value remains the accepted override.
- If neither read family is usable, an explicit genome size retains the
  `skipped_no_usable_reads` evidence path. Without reads or an explicit size,
  the stage fails with `GENOME_SIZE_UNAVAILABLE`.

Owning documents:

- `docs/architecture/reference/stage-contracts.md`
- `docs/architecture/reference/resource-handoff.md`

Focused tests must cover short-only, direct long-read input, a canonical
Read QC-produced long-read handoff overriding registry fallback,
short-over-long logical-family priority, and the explicit-size no-read skip.
The tests must assert the selected paths/refs, not infer selection from raw or
clean naming.

### 2. Remove the unused planning-preflight authority

Delete `app/stages/preflight.py` rather than integrating it.

Evidence shows it has no production caller, duplicates stage-owned runtime
resolution, and its flat requirement model cannot express Genome Survey's
short-or-long alternative plus explicit-size skip without adding a second
planning authority.

- Delete its dedicated tests.
- Remove `STAGE_CONTRACTS` and `STAGE_REQUIREMENTS` from
  `app/stages/resource_specs.py`; they are used only by the deleted module.
- Retain the runtime `ResourceSpec` constants used by stage adapters.
- Retain `validate_genome_size_execution_plan()` and its tests; it is an active
  CLI boundary and is not the unused planning-preflight module.
- Remove documentation claims that the deleted helper or stale tables remain
  part of the system.
- Preserve environment, package, bootstrap, and method preflight boundaries,
  including `app.execution.env_dispatch` and
  `app.execution.method_preflight`; they validate executable readiness and are
  unrelated to the deleted resource-planning authority.
- Add a repository-reference check proving no production or test import of
  `app.stages.preflight`, `STAGE_CONTRACTS`, or `STAGE_REQUIREMENTS` remains.

Owning documents:

- `docs/architecture/reference/stage-contracts.md`
- `docs/architecture/reference/resource-handoff.md`

### 3. Hard-cut Genome Survey ArtifactRef values

The Jellyfish database and histogram cursor fields accept only complete
`ArtifactRefJson` values.

- Remove `ArtifactRefJson | str` unions.
- Remove the GenomeScope2 string-path upgrade branch.
- Remove legacy tests that endorse string checkpoint values.
- Add a fail-closed test proving a string cursor value is rejected.
- Preserve and test the normal Jellyfish-to-GenomeScope2 indexed ArtifactRef
  chain.

Owning documents:

- `docs/architecture/reference/data-contract.md`
- `docs/architecture/reference/resource-handoff.md`

### 4. Eliminate inert Flye descriptor fields

Remove Flye's unused `accepted_asset_types` and `accepted_refs` YAML keys. The
resource resolver, not the descriptor, already owns the accepted logical input
contract, and there is no approved consumer for those keys.

The deeper defect is not specific to those two Flye keys. The descriptor
registry is an authoritative capability language, but its current Pydantic
boundary performs a lossy projection: an author can declare a field, registry
loading can succeed, and the runtime can silently discard the declaration. A
successful registry load must instead be lossless with respect to declared
keys: every key is typed and retained, or the load is rejected.

- Introduce one shared strict descriptor-model base with an explicit
  `extra="forbid"` policy and use it for every nested descriptor schema that
  participates in YAML loading, including IO, planner, conditional requirement,
  resource-hint, package, and method models.
- Keep the registry file's own accepted top-level shape explicit; unknown
  registry sections must not be silently ignored before model validation.
- Remove the inert Flye keys rather than legitimizing them as typed capability
  metadata, because resource compatibility already has one owner.
- Add a positive test loading the complete committed registry and negative tests
  for unknown keys at the registry, method, planner, IO, and package/resource
  nesting boundaries.
- Do not add a compatibility branch that strips unknown keys before validation.

Owning documents:

- `docs/architecture/reference/descriptors.md`
- root `README.md`

### 5. Fix the planner-only/local-executable invariant

Classification and implementation evidence are separate concerns. The
descriptor declares whether a capability is `local` or `planner_only`; tests
must then prove that the declared class has a real bound behavior. A second
production registry is not introduced.

Every registered method must belong to exactly one tested execution class:

- `local`: test evidence imports the actual adapter or graph builder, proves its
  lifecycle/method identity matches the descriptor ID, and exercises its frozen
  attempt or executable routing boundary;
- `planner_only`: test evidence imports the actual planner-to-stage route and
  proves it completes the accepted no-process behavior without creating or
  running a frozen attempt.

The test-evidence keys for the two classes must be disjoint and their union must
equal the complete registry. A bare method-name string in a hand-maintained set
is not implementation evidence. Local evidence must bind the ID to an imported
callable; planner-only evidence must spy on the runner and inspect the resulting
cursor/Ledger state.

The sole current planner-only method is `read_qc.length_filter.skip`. Its
accepted route passes the current long-read resource through unchanged, may
translate the canonical planner ID to the stage-internal `skip` decision label,
does not create a `FrozenAttemptSpec`, does not call a runner, and records no
attempt-started or attempt-succeeded event. Focused tests must prove that entire
translation rather than merely classifying the descriptor. These no-process
assertions are method-scoped to the length-filter skip transition: the same Read
QC stage may legitimately freeze and run its separately registered SeqKit
statistics method. The test must distinguish attempt events by method ID rather
than assert that the whole stage has no attempts. This change adds no production
registry or dependency.

Owning documents:

- `docs/architecture/reference/descriptors.md`
- root `README.md`

### 6. AutoHiC collection fails closed

AutoHiC has two separate validation boundaries:

- readiness validation occurs before freeze/run and checks only facts that can
  exist before execution: materialized inputs, approved parameters, descriptor,
  environment, packages, tools, and model files;
- output-contract validation occurs after a successful process result and
  requires all four declared review products: scaffold FASTA, `.hic`,
  `.assembly`, and HTML report.

Generated review products cannot be checked at project startup. A successful
process that omits one is an output-contract failure, not a missed readiness
check.

The Hi-C-specific collection helper must not catch a missing extra artifact and
continue to a review-ready state. A missing declared product must reach the
shared collection failure boundary. Before publishing the first review product,
collection validates the complete frozen expected-output set. An already
missing declared product therefore cannot cause an earlier product to be moved
or indexed before failure. Filesystem failure or disappearance after this
validation remains a normal collection failure handled by the same boundary;
filesystem effects are not described as transactional.

Publication helpers remain idempotent only for crash/re-entry while the durable
cursor still records the ran state. Once the shared boundary commits a collect
failure, this design adds no automatic retry route: the stage remains terminal
and any artifacts published before the late failure may remain as indexed orphan
candidates. They are not described as registered semantic outputs or as
failed-attempt evidence because the successful collection path has not emitted
their `artifact_registered` Ledger events. Recovery or orphan cleanup beyond
that point requires a separately approved workflow decision and is outside this
drift closure.

The shared collection boundary is the failure API. It returns a structured
stage update with `status=failed`, `failure_kind=collect`, and bounded causal
evidence; it does not open a new external API or turn an output-contract defect
into a human review decision.

Add a table-driven focused test for each missing scaffold FASTA, `.hic`,
`.assembly`, and HTML product. Each case removes the real declared output before
collection rather than mocking the first publication call, and must prove:

- stage status is failed with collection failure evidence;
- no review-ready candidate or adoption gate is produced;
- no attempt-succeeded Ledger event is recorded.
- none of the four review-product artifact types is newly published or indexed
  when the pre-publication completeness check finds a missing product.

Add one separate late-failure test that injects a filesystem/index failure after
the completeness check and after at least one product publication. It must prove
that the stage fails terminally, no review bundle, candidate, or adoption gate is
produced, no attempt-succeeded event is recorded, and already completed
publication is reported only as an indexed orphan candidate with no
`artifact_registered` semantic event. The test must not expect an automatic
collect retry.

Owning documents:

- `docs/architecture/topics/autohic.md`
- `docs/architecture/reference/stage-contracts.md`

## Conformance cleanup

Delete these entries only after the mapped unit's code, tests, and owning
documents meet the closure conditions:

- unit 3 closes Genome Survey legacy artifact-reference window;
- unit 2 closes Genome Survey declarative prerequisites lag the stage runtime;
- unit 4 closes Flye descriptor contains input metadata that the loader
  discards;
- unit 6 closes AutoHiC collection does not independently require every review
  artifact.

Units 1 and 5 correct request-registered drift but do not have standalone
entries on the conformance page. They must not delete an unrelated entry merely
to make all six units appear in this list.

Do not delete unrelated entries. In particular, retain the repository-quality
entry while any declared quality gate is failing.

## Verification

Verification proceeds from narrow to broad:

1. focused Genome Survey, genome-size, descriptor/registry, Read QC invariant,
   and AutoHiC tests;
2. focused mypy/ruff checks for edited modules and same-source problems;
3. declared full gates:

```bash
ruff format --check .
ruff check .
mypy app
pytest -q
```

Every reported pass must include command completion and exit-zero evidence.
Expected remaining full-suite failures are reported by root-cause cluster; they
are not reclassified as success and are not repaired outside this design. In
addition to the summary, compare exact pytest node IDs with the retained
baseline and report any added or removed IDs. Acceptance requires an empty added
set; a coincidentally unchanged failure count is insufficient. Apply the
normalized multiset and formatted-file comparison defined in the baseline
section to full ruff, mypy, and formatter output.

## Change control and rollback

- Preserve the user's existing documentation reorganization and unrelated dirty
  worktree changes.
- Do not edit old deleted architecture paths; edit the current files under
  `docs/architecture/reference/` and `docs/architecture/topics/`.
- Keep modifying work serialized because several changes share Genome Survey
  and conformance files.
- Source changes for each implementation unit are reversible by restoring only
  its explicitly owned paths; no migration or dependency installation is
  required. This is a source-level rollback claim, not a promise to erase or
  transact run data that an executable method already produced. Collection
  failure and replay semantics remain governed by the execution and data
  contracts.
