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

## Accepted changes

### 1. Canonical Genome Survey input family

The externally retained Genome Survey input family is exactly
`short_reads | long_reads`.

- `app/stages/genome_survey/nodes.py` continues to resolve both logical
  resources.
- Clean/raw priority remains an internal asset-selection rule: clean short,
  raw short, clean long, raw long.
- The checkpoint cursor, result metadata, and tests must not expose the four
  clean/raw asset types as the logical input family.
- If usable reads exist, the survey runs even when an explicit genome size is
  present; the explicit value remains the accepted override.
- If neither read family is usable, an explicit genome size retains the
  `skipped_no_usable_reads` evidence path. Without reads or an explicit size,
  the stage fails with `GENOME_SIZE_UNAVAILABLE`.

Owning documents:

- `docs/architecture/reference/stage-contracts.md`
- `docs/architecture/reference/resource-handoff.md`

Focused tests must cover short-only, long-only, short-over-long priority, and
the explicit-size no-read skip.

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

Owning documents:

- `docs/architecture/reference/resource-handoff.md`
- `docs/architecture/reference/conformance.md`

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

Make `IOSpec` explicitly reject unknown fields and add focused schema/registry
loading tests. This prevents future descriptor metadata from being silently
discarded. Do not add a new runtime compatibility mechanism.

Owning documents:

- `docs/architecture/reference/descriptors.md`
- root `README.md`

### 5. Fix the planner-only/local-executable invariant

Every registered method must belong to exactly one tested execution class:

- `local`: appears in the explicit wired-runner set and has a real executable
  adapter;
- `planner_only`: appears in the explicit no-process route set and has focused
  routing behavior that does not create or run a frozen attempt.

The two sets must be disjoint and their union must equal the complete registry.
The sole current planner-only method is
`read_qc.length_filter.skip`; its existing no-process behavior remains the
accepted route. This change strengthens registry invariant tests and does not
introduce another production registry or dependency.

Owning documents:

- `docs/architecture/reference/descriptors.md`
- root `README.md`

### 6. AutoHiC collection fails closed

After a successful execution record, collection requires all four declared
review products: scaffold FASTA, `.hic`, `.assembly`, and HTML report.

The Hi-C-specific collection helper must not catch a missing extra artifact and
continue to a review-ready state. A missing declared product must reach the
shared collection failure boundary.

Add one focused test for each missing `.hic`, `.assembly`, and HTML product.
Each test must prove:

- stage status is failed with collection failure evidence;
- no review-ready candidate or adoption gate is produced;
- no attempt-succeeded Ledger event is recorded.

Owning documents:

- `docs/architecture/topics/autohic.md`
- `docs/architecture/reference/stage-contracts.md`

## Conformance cleanup

Delete these entries only after their code, tests, and owning documents meet the
closure conditions:

- Genome Survey legacy artifact-reference window;
- Genome Survey declarative prerequisites lag the stage runtime;
- Flye descriptor contains input metadata that the loader discards;
- AutoHiC collection does not independently require every review artifact.

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
are not reclassified as success and are not repaired outside this design.

## Change control and rollback

- Preserve the user's existing documentation reorganization and unrelated dirty
  worktree changes.
- Do not edit old deleted architecture paths; edit the current files under
  `docs/architecture/reference/` and `docs/architecture/topics/`.
- Keep modifying work serialized because several changes share Genome Survey
  and conformance files.
- Each implementation unit is reversible by restoring only its explicitly
  owned paths; no migration, dependency installation, or destructive data
  operation is required.
