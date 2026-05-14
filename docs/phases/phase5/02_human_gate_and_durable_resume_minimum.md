# 02 Human Gate And Durable Resume Minimum

Task status: planned.

## Goal

Make human review gates operational across process boundaries before Phase 5
adds more long-running tools.

The minimum target is:

```text
blocked_human checkpoint
-> inspect durable state
-> submit structured decision file
-> resume same workflow
-> continue downstream
```

This task should make `assembly_review` and `hic_scaffolding` review gates
usable for real server/HPC runs without relying on in-memory state.

## Scope

- Add the smallest durable checkpoint backend required for local/server runs,
  preferably sqlite.
- Add or complete CLI lifecycle commands for:
  - `inspect`
  - `resume`
- Use structured decision files rather than free-form command flags for review
  decisions.
- Support durable resume for existing review gates:
  - `assembly_review`
  - `hic_scaffolding`
- Preserve current in-process resume validation semantics.
- Write review decisions as artifacts and ledger events.
- Continue graph execution after a valid decision.

## Non-Goals

- Do not implement full pause/retry/rerun/rebuild semantics.
- Do not implement postgres.
- Do not build a web UI.
- Do not add automatic assembly acceptance thresholds.
- Do not make purge/polish/contact-map decisions automatically.
- Do not add new bioinformatics method execution.
- Do not store large metric tables or command logs in checkpointed state.

## Decision File Contract

Use a structured JSON decision file. The exact schema can be refined during
implementation, but it should include:

```json
{
  "session_id": "phase5-review-smoke",
  "stage": "assembly_review",
  "decision": "pass",
  "candidate_id": "phase5-review-smoke:assembly.candidate.flye",
  "final_assembly_ref": "data/.../draft_assembly.fasta",
  "notes": ["human reviewed QUAST report"],
  "metadata": {
    "reviewer": "operator",
    "basis": "server-side QUAST smoke"
  }
}
```

The CLI should validate the decision file before mutating checkpoint, artifacts,
or ledger.

## Expected Outputs

- Durable checkpoint configuration for normal CLI runs.
- `gene-agent inspect --session-id ...` reports:
  - current stage
  - supervision state
  - active interrupt
  - review request artifact
  - allowed decisions
  - latest artifact refs
- `gene-agent resume --decision-file ...` can resume a blocked
  `assembly_review` or `hic_scaffolding` session.
- Review decision artifacts are written and registered.
- Ledger records review decision and lifecycle continuation.
- The workflow can continue from `assembly_review` to downstream stages after
  `pass` / `accept` / approved action decisions.

## Tasks

- [ ] Decide the durable checkpointer backend for Phase 5 minimum, with sqlite
  preferred for local/server use.
- [ ] Add checkpointer config without changing the small `TopLevelState`
  contract.
- [ ] Extend CLI parsing for `resume --decision-file PATH`.
- [ ] Add a decision file schema under `app/schemas/` or the existing review
  schema boundary.
- [ ] Load latest checkpoint by `session_id` / thread id.
- [ ] Validate active interrupt and decision compatibility before mutation.
- [ ] Apply existing `assembly_review` resume semantics through the durable
  path.
- [ ] Apply existing `hic_scaffolding` resume semantics through the durable
  path.
- [ ] Write decision artifacts and ledger events.
- [ ] Continue graph execution after resume and report final state.
- [ ] Add tests for valid resume, invalid decision, stage mismatch, missing
  checkpoint, and no duplicate side effects.
- [ ] Add a server-side review/resume smoke documented in handoff.

## Acceptance

- A server/HPC run can stop at `blocked_human`, exit the process, and later
  resume from a decision file.
- `resume` does not restart from inventory or create a fresh initial state.
- Invalid decisions fail before mutating checkpoint, artifacts, or ledger.
- Review decisions remain auditable through artifacts and ledger.
- Generated assemblies and Hi-C scaffold candidates still require explicit human
  decisions.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_review_resume tests.test_entrypoints -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
