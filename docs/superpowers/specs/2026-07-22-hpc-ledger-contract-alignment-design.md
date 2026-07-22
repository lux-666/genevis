# HPC Ledger Contract Alignment Design

## Goal

Make the HPC smoke tests match the current workflow and ledger v2 contracts, and
prevent logical asset identifiers from being serialized as durable artifact
paths. A server checkout should be able to pull the change and rerun bounded
read-QC, genome-survey, assembly, review, Hi-C, and T2T tests without false
failures caused by stale test helpers.

## Scope

- Correct nested ledger reference extraction for asset identifiers.
- Add regression coverage for asset value references and retention auditing.
- Bound HPC runs with `requested_stages` as well as `local_exec`.
- Update HPC ledger assertions for the raw ledger v2 envelope.
- Update the genome-size handoff assertion for the provenance-aware contract.
- Improve assertion diagnostics without changing production workflow policy.

The change does not relax artifact-retention validation and does not alter the
biological interpretation of the supplied HPC data.

## Reference Semantics

`Ledger.references_from()` currently treats every key ending in `_ref` as a
path. That converts values such as
`session:raw_long_reads:reads.fastq` into invalid `path_ref` entries. Reference
extraction will distinguish two durable forms:

- Artifact and file references remain `{role, path_ref}` records.
- Logical asset identifiers from keys ending in `_asset_ref` or `_asset_refs`
  become `{role, value}` records.

Artifact paths continue to be normalized by `SessionPaths`; no audit checks are
weakened. The retention scanner naturally ignores value-only references while
preserving them as bounded ledger evidence.

Existing v2 ledgers may already contain roles ending in `_asset_ref` encoded as
`path_ref`. Retention auditing will recognize only those legacy asset-identity
roles as non-artifact values. Artifact roles, `_artifact_ref` roles, and normal
artifact paths remain strict and continue to fail on missing or conflicting
index records.

## HPC Contract Updates

The shared run-config writer will set both `local_exec` and
`requested_stages` to its supplied stages by default. This makes single-stage
and selected-stage smoke tests stop after their declared targets instead of
falling into an unconfigured next stage. Test 04 will then append
`assembly_dedup` to `requested_stages` without adding it to `local_exec`, because
dedup is an automatic stage rather than a `LocalExecStage`. Test 09 may continue
to overwrite `requested_stages` with its explicit end-to-end plan. The dedicated
genome-survey writer will request only read-QC and genome-survey.

Raw ledger v2 stores method names below `summary` and attempt identity in
`subject_id`. HPC assertions will read those fields, with compatibility fallback
to projected/top-level fields. Failure output will list matching attempt rows so
a remote failure is diagnosable from the per-step log.

The genome-size handoff check will provide both `genome_size_prior` and
`genome_size_source="survey"`, matching the canonical resolver contract.

## Error Handling

Unknown or malformed artifact paths must continue to fail closed. Only fields
whose key explicitly denotes an asset identity are converted to value refs.
The implementation will not infer reference type from punctuation in the
value, because colons and slashes may occur in either IDs or paths.

The supplied HPC ledger proves that asset identities are currently encoded with
the wrong reference kind, but it does not include `artifacts/index.jsonl`, so it
cannot by itself identify the exact index conflict that triggered terminal
compaction. Audit exceptions will therefore include the offending context,
role, artifact ID, and path. The asset-reference fix and improved diagnostics
are both required; the design does not claim that an unobserved index mismatch
is repaired without a reproducer.

## Verification

- Unit tests for singular and plural asset value refs, asserting `value` is
  present and `path_ref` is absent.
- A regression path covering `references_from` to a persisted ledger event and
  then `verify_checkpoint_free_artifact_retention`.
- A compatibility test showing a legacy `_asset_ref` role encoded as `path_ref`
  remains readable without weakening validation for artifact roles.
- Audit error-message coverage that identifies the specific conflicting
  reference.
- Existing ledger, storage-audit, checkpoint-compaction, state, and HPC-related
  unit tests.
- `bash -n` for the modified HPC scripts.
- Generate representative run-config JSON and verify `local_exec`,
  `requested_stages`, and stage-specific settings.
- `git diff --check` before commit.
