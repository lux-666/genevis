# Task 02: Align T2T Execution and Provenance with the Shared Lifecycle

**Phase**: 13
**Status**: Planned
**Depends on**: Task 01

## Goal

Complete the T2T minimap2 `freeze -> run -> collect` contract so that its
execution, output publication, ledger facts, recovery behavior, and downstream
candidate evidence match the shared execution lifecycle used by the mature
stages.

## Current State

T2T already invokes `build_method_graph(...)` and currently enters the shared
run kernel through a direct attempt. The remaining inconsistency is in the
stage adapter:

- freeze places the raw work-path PAF in `alignment_paf_ref`;
- collect reads that raw path instead of publishing and loading an immutable
  PAF artifact;
- the PAF is not registered as the method output;
- collect does not record `attempt_succeeded`;
- candidate evidence is not explicitly bound to the verified frozen attempt
  and execution evidence;
- lifecycle recovery and collect-failure cases are not covered to the same
  depth as Assembly and Hi-C.

## Design

### Freeze boundary

`freeze_t2t_alignment()` owns command construction only. It must:

1. resolve the selected assembly and combined telomeric-read FASTA;
2. create the stage work directory and minimap2 script;
3. declare the work PAF as an expected output in `FrozenAttemptSpec`;
4. persist complete method metadata required by collect;
5. bind the frozen attempt through the configured lifecycle owner.

Freeze must not publish the PAF and must not expose its work path as a durable
stage artifact ref. The frozen attempt is the recovery relation for that path.

The collect implementation must stay owner-neutral by using
`load_method_executions(...)`. Task 03 will change the lifecycle from a direct
attempt owner to `ParameterAttemptOwner` without creating a second collect
path.

### Collect boundary

`build_t2t_candidates()` becomes the T2T collect adapter. It must:

1. load exactly one verified execution through the shared lifecycle;
2. require a successful process outcome before reading biological output;
3. resolve the expected PAF from the verified frozen attempt, not from a raw
   stage field;
4. validate that the PAF exists and is readable;
5. publish the PAF from work storage as immutable artifact type
   `t2t.read_alignment_paf`;
6. register the PAF and candidate table in the artifact index;
7. construct candidates from the immutable PAF ref;
8. bind candidate evidence to the frozen-attempt ref and execution-evidence
   ref selected by the lifecycle owner;
9. record `attempt_succeeded` only after required output publication and
   candidate construction succeed.

The stage output should retain compact refs and counters. It must not duplicate
full frozen specs, execution bodies, or absolute work paths already owned by
the lifecycle records.

### Recovery semantics

- Resume from `alignment_frozen` enters run without rebuilding the script.
- Resume from `alignment_ran` enters collect without executing minimap2 again.
- Re-entry after candidate construction returns the existing immutable refs and
  does not republish or overwrite evidence.
- Ordinary minimap2 process failure remains a bounded stage failure according
  to the lifecycle policy.
- Publication, artifact-index, ledger, or verified-ref failures stop collect;
  they do not produce a successful candidate bundle.

## Implementation Surface

- `app/stages/t2t/nodes.py`
- `app/schemas/artifacts.py`
- shared artifact registration helpers where an existing helper is sufficient
- `tests/test_t2t.py`
- `tests/test_execution_lifecycle.py`

Do not create a T2T-local runner wrapper, execution evidence format, lifecycle
factory, or duplicate frozen/execution cursor.

## Evidence Contract

The resulting T2T output must make this relation auditable:

```text
lifecycle owner
  -> frozen attempt ref
  -> execution evidence ref
  -> published PAF ref
  -> candidate table ref
  -> patch/review evidence
```

The candidate artifact or its manifest must carry immutable bindings to the
selected attempt and execution. A raw `work/t2t/.../ont_to_assembly.paf` path is
not sufficient provenance.

## Tests

- Freeze writes one complete minimap2 frozen attempt.
- Resume after freeze does not rebuild the command.
- Resume after run does not run minimap2 again.
- Collect publishes and registers the PAF.
- Candidate construction reads the immutable PAF ref.
- Successful collect records `attempt_succeeded`.
- Missing PAF, malformed PAF, publication failure, and ledger failure cannot
  produce a successful collect state.
- Candidate evidence is bound to the exact frozen and execution refs.
- Collect re-entry is idempotent.

## Acceptance Criteria

- [ ] T2T minimap2 uses only the shared execution lifecycle.
- [ ] No durable stage field treats a work PAF path as an artifact ref.
- [ ] PAF publication and registration are complete.
- [ ] Candidate evidence is attempt- and execution-bound.
- [ ] `attempt_succeeded` is written after successful collect.
- [ ] Freeze/run/collect replay tests pass.
- [ ] Collect failure tests prove fail-closed provenance behavior.
