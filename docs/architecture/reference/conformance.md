# Architecture Conformance

This page records known differences between accepted architecture and the
current working tree. It is not a compatibility contract and does not weaken
the owning reference specifications.

> **Status:** conformance-debt  
> **Read when:** a design claim and current code appear to disagree.  
> **Comes from:** architecture review and repository verification.  
> **Feeds into:** scoped implementation work that closes a listed gap.  
> **Related topics:** [Data and storage](../topics/data-and-storage.md),
> [Execution](../topics/execution.md),
> [Runtime orchestration](../topics/runtime-orchestration.md), and
> [Bootstrap](../topics/bootstrap.md).  
> **Code evidence:** each item below names its current evidence path.

## Open gaps

### Genome Survey legacy artifact-reference window

The accepted data contract is a hard cutover to index-backed
`ArtifactRefJson` values at executable artifact-dependency boundaries. The
Genome Survey cursor still accepts `ArtifactRefJson | str` for the Jellyfish
database and histogram, and the GenomeScope2 freeze boundary still upgrades
legacy string paths.

- Current evidence: `app/stages/genome_survey/state.py` and
  `tests/test_genome_survey.py`.
- Contract owner: [Data contract](data-contract.md) and
  [Resource handoff](resource-handoff.md).
- Closure condition: remove the string union, upgrade branch, and legacy test,
  or explicitly approve a bounded compatibility policy in the owning contract.

Until that decision is made, new producers must emit complete indexed artifact
references. This debt entry must not be cited as permission for new string-ref
producers.

### Genome Survey declarative prerequisites lag the stage runtime

The Genome Survey stage resolves both `short_reads` and `long_reads`, runs a
full survey with either family, and deliberately prefers clean short, raw
short, clean long, then raw long reads. With neither family, an explicit genome
size permits its documented skip path. `STAGE_CONTRACTS` and
`STAGE_REQUIREMENTS` still declare only `long_reads`. The read-only
planning-preflight module consumes those tables, although no production entry
point currently calls that preflight API.

- Current evidence: `app/stages/genome_survey/nodes.py`,
  `app/stages/resource_specs.py`, `app/stages/preflight.py`, and
  `tests/test_genome_survey.py`.
- Contract owner: [Stage contracts](stage-contracts.md) and
  [Resource handoff](resource-handoff.md).
- Closure condition: express and test the one-of short/long requirement in the
  central declarative API, or remove that unused duplicate contract and make
  the stage-owned resolver boundary the only authority.

### Repository quality gates are targets, not a current success claim

The repository intends these commands to be merge gates:

```bash
ruff format --check .
ruff check .
mypy app
pytest -q
```

The working tree must not be described as passing a gate unless that command
returns zero in the revision being described. Historical error counts are not
retained here because they become stale immediately.

- Policy owner: [Governance](governance.md).
- Current evidence: `pyproject.toml` plus the command output for the checked
  revision.
- Closure condition: all four commands return zero and CI or an equivalent
  enforced workflow runs them without exclusions that weaken the declared
  policy.

### AutoHiC collection does not independently require every review artifact

The AutoHiC frozen attempt declares the scaffold FASTA, `.hic`, `.assembly`, and
HTML report as expected outputs, and the normal generated script fails if they
are missing. The collection helper nevertheless skips a missing contact map,
assembly file, or HTML report and can still construct review-ready output. That
does not satisfy the stage contract's general rule that missing expected
outputs are explicit failures at the collection boundary.

- Current evidence:
  `app/stages/hic_scaffolding/method_application/support.py` and
  `app/stages/hic_scaffolding/method_application/autohic.py`.
- Contract owner: [Stage contracts](stage-contracts.md) and
  [Execution lifecycle](execution-lifecycle.md).
- Closure condition: collection rejects a missing declared review artifact, or
  the owning contracts explicitly narrow which AutoHiC outputs are mandatory.

### Storage garbage collection is audit-only

The run data contract defines retention and garbage-collection safety, but the
implemented `storage-audit` command is read-only. It can project a cleanup
manifest; it does not delete unreachable immutable artifacts, compact the
artifact index after deletion, or reclaim retained method work directories.

- Current evidence: `app/storage/audit.py` and the `storage-audit` CLI path in
  `app/entrypoints/run.py`.
- Contract owner: [Run data](data-contract.md).
- Closure condition: implement and test an explicit safe apply path, or narrow
  the accepted contract to audit and cleanup-manifest generation only.

### Flye descriptor contains input metadata that the loader discards

The Flye method descriptor declares `accepted_asset_types` and `accepted_refs`
under its `long_reads` input. `IOSpec` does not declare either field, and the
descriptor models currently use Pydantic's default extra-field behavior, so
both values are silently discarded during registry loading. They must not be
cited as runtime-enforced compatibility rules.

- Current evidence: `descriptors/methods/assembly/flye_candidate.yaml` and
  `app/schemas/descriptors.py`; a loaded `IOSpec.model_dump()` contains only
  `name`, `type`, `description`, and `required`.
- Contract owner: [Descriptors](descriptors.md).
- Closure condition: add and consume typed fields with focused tests, or remove
  the inert YAML keys. If strict rejection is the intended general policy, set
  and test an explicit extra-field policy for descriptor models.

### Some reports still depend on live state projections

The run-data contract says reports are regenerable projections from
authoritative artifacts and Ledger events. The session summary satisfies that
boundary, but `write_operator_report` requires a caller-supplied compact result
for current stage, supervision, error, interrupt, and route tail. The
stage-owned `assembly_review.md` writer likewise renders from the live Assembly
Review cursor and an indexed packet; it neither embeds the shared source
watermark nor has a dedicated durable-record-only regeneration path. There is
no command or loader that reconstructs either exact projection solely from the
Ledger and indexed artifacts. Despite that, `_reclaimable_candidates` currently
labels every file under `reports/` as a derived report that can be regenerated.
The cleanup manifest is dry-run-only today, but that blanket classification is
not safe input for a future apply path.

- Current evidence: `app/reports/review_report.py` versus
  `app/reports/trace_writer.py`, plus the report helpers in
  `app/stages/assembly_review/nodes.py`, `_reclaimable_candidates` in
  `app/storage/audit.py`, `tests/test_run_trace.py`, and
  `tests/test_assembly_review.py`.
- Contract owner: [Run data](data-contract.md).
- Closure condition: implement and test durable-record reconstruction and
  equivalent watermarks for both projections, or explicitly narrow the owning
  contract so these live-result projections are not promised to be
  independently regenerable; in either case, derive cleanup eligibility from
  proven per-report regeneration support instead of directory membership.

### Run-level replay capability is not declared

The data contract defines `fully_materialized`,
`verified_external_dependencies`, and `audit_only` as the run-level execution
replay vocabulary. `SessionPaths` exposes `control/run.json`, but no production
writer creates that file and no current loader consumes a replay-capability
field. Frozen control inputs and per-attempt dependency records provide the
underlying evidence, but there is no single durable declaration to query.

- Current evidence: `app/storage/paths.py`, `app/storage/frozen_inputs.py`, and
  `app/execution/dependency_snapshot.py`; repository search finds no writer or
  reader of `run_metadata_path` outside its property definition.
- Contract owner: [Run data](data-contract.md).
- Closure condition: publish and validate a run metadata record whose replay
  enum is derived from the frozen dependency/materialization facts, or remove
  the enum and `run.json` target from the accepted contract and path API.

## Update rule

Every entry must identify the accepted owner, current code evidence, and an
objective closure condition. Delete an entry in the same change that closes it;
do not turn this page into a backlog, phase report, or historical status log.
