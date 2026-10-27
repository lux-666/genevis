# Offline verification walkthrough

This walkthrough checks Gene Agent's workflow mechanisms using existing synthetic
regression fixtures. It requires the Python development dependencies, but no
sequencing dataset, Conda biological-tool environments, BUSCO/NCBI databases, or
LLM API credentials. Dependency installation may require network access.

It is a test-driven walkthrough, not a real genome assembly demonstration. The
fixtures create temporary inputs and session directories, exercise production
storage and control code, and remove those directories when each test finishes.
Some execution and environment boundaries use test doubles.

## Install from a checkout

Use Python 3.11 or newer, from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints/ci.txt -e '.[dev]'
gene-agent --help
```

Python dependencies are defined in [pyproject.toml](../../pyproject.toml).

## Verify input, decision, recovery, and audit behavior

Run these five existing tests:

```bash
python -m pytest -v \
  tests/test_inventory.py::InventoryStageTests::test_inventory_succeeds_with_valid_manifest \
  tests/test_durable_resume.py::DurableResumeTests::test_durable_resume_assembly_review_accept_advances \
  tests/test_durable_resume.py::DurableResumeTests::test_invalid_decision_leaves_blocked_state_intact \
  tests/test_storage_audit.py::StorageAuditTests::test_structured_audit_is_read_only_and_reports_reachability \
  tests/test_storage_audit.py::StorageAuditTests::test_registered_artifact_integrity_mismatch_fails_closed
```

Success means all five selected tests pass with exit code zero. A skip, failure,
or collection error needs investigation; it is not a successful walkthrough.

| Check | Input / action | Verified result |
|---|---|---|
| Input registration | Synthetic manifest and read files | Inventory registers the supplied assets and advances |
| Durable review recovery | Synthetic assembly candidate, frozen inputs, SQLite checkpoint closed and reopened; bound `accept` decision | The review decision and resume lifecycle are recorded in Ledger, and the workflow advances beyond `assembly_review` |
| Invalid decision | A decision that does not satisfy the active review request | Recovery rejects it and preserves the blocked state |
| Read-only audit | Indexed, referenced, orphan, and unindexed fixture artifacts | Audit reports reachability and storage layers without changing the session tree |
| Artifact integrity | A registered artifact whose bytes have changed | Audit rejects the hash mismatch |

The durable recovery fixture contains a four-base synthetic FASTA (`ACGT`), not
an assembly produced by Flye. Its candidate identifier is a fixture identity.
Advancing beyond a review gate does not establish that every downstream stage
completed. Read the assertions in [the recovery tests](../../tests/test_durable_resume.py)
and [the audit tests](../../tests/test_storage_audit.py) for the exact scope.

## Move to a real workflow

Use [new host setup](../deployment/migration.md), then follow the existing
[acceptance order](run-and-recovery.md#acceptance-order-code-tests-before-real-end-to-end-runs).
The [configuration templates](../examples/) describe supported inputs. A public
real-data demonstration must also identify its input source/checksums, tool and
database versions, resources, executed methods, decisions, and output metrics.
Neither this walkthrough nor CI establishes assembly quality, recommendation
quality, or compatibility with every tool version.

For installation failures or trial feedback, use the
[contribution guide](../../CONTRIBUTING.md).
