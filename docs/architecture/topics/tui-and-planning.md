# TUI and Planning

> **Status:** implemented for raw HiFi/ONT and paired Hi-C intake; broader
> midstage intake is planned separately.  
> **Read when:** changing conversational intake, Plan Draft behavior,
> whole-run freeze, or draft recovery.  
> **Comes from:** operator goal text, confirmed sample facts, descriptor-derived
> method catalog, and machine configuration prepared by [bootstrap](bootstrap.md).  
> **Feeds into:** immutable frozen inputs and
> [runtime orchestration](runtime-orchestration.md).  
> **Contract dependencies:** [run data](../reference/data-contract.md),
> [descriptors](../reference/descriptors.md), and
> [stage contracts](../reference/stage-contracts.md).  
> **Code evidence:** `app/entrypoints/tui.py`, `app/entrypoints/tui_intake.py`,
> `app/entrypoints/ask_planner.py`, `app/entrypoints/planner_catalog.py`,
> `app/schemas/tui_intake.py`, and `app/storage/frozen_inputs.py`.

## Mutable draft boundary

`gene-agent tui` is an optional conversational new-run entry. Before
confirmation it owns a versioned mutable draft with this lifecycle:

```text
intake -> planning -> ready_to_confirm -> frozen
                                      -> running
                                      -> blocked_review | terminal
```

The draft is not a graph checkpoint and is not an executable contract. It can
be revised or resumed while unfrozen. The only top-level TUI command during
normal conversation is `/resume`, which selects an existing draft or blocked
session through the appropriate recovery path.

For real/operator runs with already prepared files, use the deterministic
boundary instead: `gene-agent run --manifest PATH --run-config PATH --data-dir
PATH [--non-interactive]`. Its file-authored run config must explicitly contain
JSON arrays for `requested_stages` and `local_exec`; `requested_stages: null` is
rejected, while `[]` is valid. The command validates/canonicalizes both files and
converges on the same whole-run freeze and SQLite executor below. `session_id`
belongs only to the manifest. Internal/TUI compatibility state may still use
`None`, but that is not a legal public structured-run value.

Current intake directly models PacBio HiFi reads, ONT reads, explicit paired
Hi-C lanes, an explicit no-Hi-C fact, species facts, and a telomere motif when
supplied. It does not currently collect clean long reads, an existing assembly,
CLR, CycloneSEQ, short reads, or annotation evidence. Backend manifest and
inventory support must not be mistaken for TUI support; see the
[planned midstage node](tui-midstage-entry.md).

## Intent is not the execution plan

The intent parser decides which facts the TUI still needs to ask about. It does
not authorize tools or own the final stage set. Once intake is complete, the
planner receives the confirmed manifest view and a method catalog derived from
the descriptor registry. It produces the editable Plan Draft: stages, methods,
planning-time parameters, and attention items.

The operator can edit stages, methods, and appropriate parameters before the
final confirmation. Runtime recommendations that require upstream evidence
remain deferred. If the planning model fails or returns an invalid draft, the
planning attempt stops and the durable draft remains resumable; the TUI does
not invent a deterministic fallback stage plan.

## Whole-run freeze

Final confirmation invokes the same transaction-like publication boundary used
by the public structured `run` command:

```text
confirmed draft
  -> input_manifest.yaml
  -> run_config.json
  -> dependency_manifest.json
  -> frozen_inputs.json commit marker
  -> execute_frozen_session(session_id)
```

The marker is written last and binds the hashes and identities of the other
documents. An identical repeated freeze is idempotent. A changed request,
partial freeze, corrupt hash, or existing execution evidence without a valid
checkpoint fails closed instead of silently rebuilding inputs.

The dependency snapshot captures the selected machine closure but deliberately
does not run Conda probes. Bootstrap owns readiness probing. After freeze, the
executor resolves from the snapshot rather than later edits to
`config/envs.yaml`.

Whole-run freeze does not generate every future method command. Each selected
stage later creates the narrower method-attempt freeze described in
[Method execution](execution.md).
