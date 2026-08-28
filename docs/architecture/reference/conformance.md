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

### Repository quality gates are targets, not a current success claim

The repository intends these commands to be merge gates:

```bash
ruff format --check .
ruff check .
mypy app
pytest -q
```

The working tree passes a gate only when that command returns zero for the
revision being described; the command output is the evidence.

- Policy owner: [Governance](governance.md).
- Current evidence: `pyproject.toml` plus the command output for the checked
  revision.
- Closure condition: all four commands return zero and CI or an equivalent
  enforced workflow runs them without exclusions that weaken the declared
  policy.

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

This is report-regeneration debt only. Projection exception isolation is
implemented: `ProjectionBoundary` converts ordinary report/projection failures
into bounded warnings and they do not alter lifecycle, review status,
checkpoint, or authoritative-terminal facts. It is therefore not an open
conformance gap.

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
replay vocabulary and reserves `control/run.json` as its future owner, but no
production path API, writer, or loader currently implements that record.
Frozen control inputs and per-attempt dependency records provide the underlying
evidence, but there is no single durable declaration to query.

- Current evidence: `app/storage/frozen_inputs.py` and
  `app/execution/dependency_snapshot.py`; repository search finds no
  `control/run.json` path API, writer, or reader.
- Contract owner: [Run data](data-contract.md).
- Closure condition: publish and validate a run metadata record whose replay
  enum is derived from the frozen dependency/materialization facts, or remove
  the enum and `run.json` target from the accepted contract and path API.

### Five-stage real-tool contig acceptance is not automated

The five-stage raw-read contig path has focused unit and integration coverage,
including whole-graph lifecycle tests, but it does not have one automated
acceptance run that invokes the real SeqKit, Jellyfish, GenomeScope2, selected
assembler, purge_dups, QUAST, BUSCO, and configured LLM endpoint on one
representative input through canonical completion. Most method tests isolate
their stage with fixtures, stubs, or patched external boundaries. The existing
real-tool Read QC smoke does not close the whole-path gap.

`server_check.sh` proves the fixed host tool/package/database closure and
`server_test.sh` proves code regression only. Neither command is biological
execution evidence, and neither may be cited as a successful contig run.

- Current evidence: `tests/test_read_qc.py`, `tests/test_genome_survey.py`,
  `tests/test_assembly.py`, `tests/test_assembly_dedup.py`,
  `tests/test_assembly_review.py`, `scripts/server_check.sh`, and
  `scripts/server_test.sh`.
- Contract owners: [Stage contracts](stage-contracts.md) and the
  [deployment boundary](../../deployment/README.md).
- Closure condition: add an explicit server acceptance harness that uses the
  canonical contig templates, installed real tools, a configured live LLM, and
  representative server-local reads; require a canonical completed terminal,
  five settled analytical occurrences, and a hash-verified `final_assembly`
  from detached inspection. Focused stubbed tests do not satisfy this condition.

## Update rule

Every entry identifies the accepted owner, current code evidence, and an
objective closure condition. Delete an entry in the same change that closes it.
