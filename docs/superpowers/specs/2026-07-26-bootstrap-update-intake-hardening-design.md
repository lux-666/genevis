# Bootstrap Update and TUI Intake Hardening

## Status and Scope

This change hardens the existing TUI/bootstrap/frozen-input implementation. It
does not add new workflow roles, configuration fields, installers, directory
discovery, or public execution commands.

The work covers:

- one-step interactive commit semantics for `gene-agent bootstrap --update`;
- validation of the complete candidate machine configuration before it is
  written;
- exact path and role handling during TUI sample intake;
- explicit replacement of confirmed Hi-C pairs;
- recovery of a frozen session when no executor still owns it;
- dependency snapshots restricted to the selected run plan; and
- secure `.env` permissions on both creation and update.

## Bootstrap Command Contract

The two bootstrap modes keep different commit contracts:

```text
gene-agent bootstrap [--apply]
gene-agent bootstrap --update
```

Normal bootstrap retains `--apply` for existing non-interactive automation and
migration scripts. Interactive update does not require `--apply`; combining
`--update` and `--apply` is rejected as conflicting usage.

`bootstrap --update` performs one interactive transaction:

```text
select managed fields
  -> enter replacement values
  -> build an in-memory ProjectBootstrapPlan
  -> validate the complete candidate config with runtime config/package rules
  -> show every old -> new change and validation result
  -> prompt: Enter applies; Ctrl-C/Ctrl-D cancels
  -> atomically publish config and .env updates
  -> run declared conformance actions
```

Only an empty confirmation response commits. Any other text repeats the final
prompt. `KeyboardInterrupt` or `EOFError` before publication returns without
writing config, `.env`, or conformance changes.

The plan is authoritative: every changed config or `.env` field must have one
`ProjectConfigChange`. Update mode must not repair, replace, or normalize an
unselected field. In particular, recursive placeholder replacement remains an
initialization/repair behavior and is not run for explicit update plans.

## Bottom-Level Validation and Publication

Runtime configuration parsing must have one implementation. `EnvResolver`
will support construction from already parsed config data as well as from a
path. Both construction paths use the same internal validation and model
loading code.

Before confirmation, the bootstrap plan is validated without publishing it:

- the candidate mapping must load through `EnvResolver`;
- bootstrap-owned path fields must pass the same existence/model checks used
  by `verify_envs_yaml`;
- declared package roots and canaries must pass package preflight, except for
  paths explicitly produced by a declared conformance action; and
- unresolved or invalid values abort before the commit prompt.

`apply_project_bootstrap_plan` revalidates the plan so programmatic callers
cannot bypass the boundary. Config and `.env` files are staged in their target
directories and replaced only after validation. Publication is atomic per
file; it is not represented as a cross-file transaction. Existing `.env`
content is preserved except for approved `GENE_AGENT_*` updates. Its mode is
forced to `0600` after both creation and replacement. A conformance failure
after publication returns nonzero, retains the already committed config/`.env`
changes, and still reports the final package-preflight result.

## TUI Intake Corrections

Path extraction preserves the complete token from the raw answer. It never
truncates a token at an embedded supported suffix. Therefore
`reads.fastq.bak` is validated as that exact path and rejected for its suffix;
it cannot become `reads.fastq`.

Focused-role fallback applies only to otherwise unlabelled text. A path in a
clause explicitly labelled Hi-C but lacking explicit R1/R2 attribution remains
unresolved, including while the current question asks for ONT reads.

Confirmed facts are not silently extended or replaced:

- a new ONT value is unresolved until replacement is confirmed;
- a new Hi-C pair is unresolved until replacement is confirmed;
- confirmed Hi-C replacement replaces the existing pair collection rather
  than appending to it; and
- filename-derived pairing remains forbidden.

The current boolean `replace_confirmed` remains at the merge boundary only if
it has exact replacement semantics for both ONT and Hi-C. No extra TUI wrapper
or secondary fact store is introduced.

## Frozen Execution Recovery

Executor ownership is represented by an advisory lock below the session
control directory. The internal `execute_frozen_session` acquires the lock for
the complete execution attempt. A second process that cannot acquire it treats
the session as actively executing and refuses duplicate execution. Process
exit releases the lock automatically.

After frozen-input integrity validation, `/resume` routes as follows. Both a
fresh start and a checkpoint continuation call the same frozen executor with
the durable SQLite checkpointer and never rematerialize control inputs.

| Durable state | Action |
|---|---|
| execution lock held | Refuse duplicate execution or input edits |
| no lock, no execution checkpoint or execution artifacts | Start from the immutable frozen inputs |
| no lock, resumable nonterminal SQLite checkpoint | Continue the same session from that checkpoint |
| no lock, partial execution evidence without a resumable checkpoint | Refuse a fresh restart and report that recovery is unavailable |
| blocked human review | Enter the existing review path |
| completed or failed terminal | Offer a child rerun draft |

This closes the crash window between publishing `frozen_inputs.json` and
starting the graph without rerunning completed work after a later crash.
Resume never mutates the frozen control inputs.

## Dependency Snapshot Scope

The confirmed planner passes its resolved execution order and selected methods
into the freeze boundary alongside the run-config draft. The freeze boundary
validates the run config first and verifies that the supplied stage selection
agrees with its requested/local-execution scope. It then builds the execution
dependency closure: explicitly selected methods plus any registered method
choices that a selected stage is allowed to make after freeze. Dynamic choices
are therefore bounded by the selected stage instead of being inferred from a
`RunConfig` that does not retain general method selections.

Dependency snapshot capture receives this validated closure. The snapshot
contains the default environment plus only stage/method entries in the closure
and only packages referenced by those resolved specs. It does not serialize
methods from unrelated stages, unrelated configured packages, or `.env`
values. The snapshot's stage and method keys durably record the closure used at
execution time.

The frozen resolver continues to fail closed if a selected spec references a
package absent from the snapshot. Execution never falls back to live
`config/envs.yaml`.

## Error Handling

- Invalid update selection or values return a nonzero status without writes.
- Candidate validation errors are shown before the final confirmation prompt.
- Cancellation is not reported as a successful update.
- Atomic publication failures return nonzero and leave the previous file
  content usable wherever the operating system completed no replacement.
- A held execution lock is reported distinctly from damaged frozen inputs.
- Failed frozen recovery updates the TUI lifecycle using the same durable
  lifecycle inspection used after initial execution.

## Tests

Focused tests must cover:

1. `bootstrap --update` commits on final Enter without `--apply`;
2. `--update --apply` is rejected while normal `bootstrap --apply` remains;
3. Ctrl-C/EOF and invalid candidate paths produce no file changes;
4. every update mutation appears in the rendered plan;
5. update mode leaves unselected placeholders untouched;
6. existing `.env` files become mode `0600` after a secret update;
7. `reads.fastq.bak` is not truncated or accepted as `reads.fastq`;
8. explicit unlpaired Hi-C text is never accepted as ONT;
9. Hi-C replacement requires confirmation and removes old pairs;
10. a held execution lock refuses duplicate execution;
11. an unlocked frozen-but-not-started session starts execution;
12. an unlocked nonterminal checkpoint resumes without fresh execution, while
    partial execution without a resumable checkpoint fails closed;
13. selected dependency closures omit unrelated-stage methods/packages while
    retaining allowed post-freeze choices for selected stages; and
14. the existing frozen hash, child rerun, and live-config isolation tests
    remain green.

Verification runs `ruff check app tests`, the focused bootstrap/TUI/frozen
tests, and the complete unit-test suite.
