# 04 Review Policy: Strict / Auto Modes And LLM Recommendation

Task status: implemented locally; full test suite passing (325/325). Server
acceptance for the auto-mode final-report path is pending — the server smoke
script `scripts/server_phase5_auto_review_smoke.sh` is prepared but has not
been executed on the HPC host yet. Depends on Phase 5 task 02 (durable
resume) being complete — it is. Does not require Phase 6.

## Goal

Make human review gates usable for users who cannot babysit a multi-day
genome workflow, without quietly making biological decisions on their
behalf.

The behavior is selected per session by a single user-facing knob:

```text
review_policy = strict | auto
```

Strict mode preserves the current "stop, wait, resume" semantics and adds
an explicit waiting strategy. Auto mode advances the workflow using
recorded provisional decisions backed by an LLM rationale artifact, and
flags the small subset of checkpoints whose correctness cannot be
recovered after the fact for explicit human confirmation in the final
report.

The principle is **auditability over gating**. Every decision — human,
LLM, or rule — must produce:

- inputs read (artifact refs)
- rationale text
- the decision itself
- the policy that produced it (`decided_by`)

All four must be in the ledger and reachable from `gene-agent inspect`.
A user who runs in `auto` mode and reviews the run a week later should
see the same evidence as a user who reviewed each gate live.

## Scope

- Add a `review_policy` block to the run-config schema. Per-stage
  overrides are allowed but optional; the top-level mode is the default.
- Extend the existing `active_interrupt` / review-request flow with a
  pre-block decision step that consults the policy.
- Add an LLM recommendation prompt bundle for review checkpoints. The
  LLM reads the existing review-request artifact (signals, candidates,
  decisions_allowed) and writes a recommendation artifact. It does not
  write ledger events and does not mutate state.
- Add per-checkpoint `auto_decidable: bool` metadata in the review
  request payload. A checkpoint that is not auto-decidable always blocks
  in `auto` mode and surfaces in the final report as
  "must be confirmed by a human".
- Wire the existing `resume_review_state(blocked_timeout_seconds=...)`
  helper through to the CLI as `--max-blocked-seconds` (the future-work
  item left by task 02), and into `auto` mode as the default when no
  explicit timeout is given.
- Extend `ResumeReviewRequest` with a `decided_by:
  "human" | "llm_provisional" | "auto_rule"` discriminator and a
  required `rationale_artifact_ref` when `decided_by != "human"`.
- Extend `gene-agent inspect` so a session can be filtered by
  `decided_by`, surfacing every provisional decision in one place.
- Produce a final-report artifact at workflow termination that lists
  every decision with `decided_by`, decision value, and
  `rationale_artifact_ref`. Auto-mode runs must surface the
  not-auto-decidable checkpoints at the top.

## Non-Goals

- Do not add biological accept/reject thresholds. The LLM may
  *recommend* `pass` / `purge` / `polish`, but the threshold itself is
  not encoded.
- Do not let the LLM write ledger events, mutate state, or produce
  artifacts other than its own recommendation document.
- Do not let `auto` mode advance through a checkpoint whose
  `auto_decidable` is `false` in the descriptor. Those still block, but
  the decision is recorded as `pending_human_confirmation` rather than
  `provisional`.
- Do not introduce a new persistence backend; reuse the sqlite
  checkpointer landed in task 02.
- Do not introduce a "high-risk / low-risk" classification. The
  framework is review *policy*, not risk policy.
- Do not change `TopLevelState` shape beyond what the durable resume
  flow already supports.
- Do not add a web UI.

## Decision Path

```text
stage produces review_request artifact
  -> review.policy.evaluate(stage, request, run_config) returns one of:
       BLOCK_INDEFINITE        (strict, no timeout, current behavior)
       BLOCK_WITH_TIMEOUT      (strict, timeout > 0)
       AUTO_DECIDE_NOW         (auto, checkpoint is auto_decidable)
       BLOCK_PENDING_CONFIRM   (auto, checkpoint is not auto_decidable)
  -> when AUTO_DECIDE_NOW:
       LLM produces recommendation artifact
       a synthetic ResumeReviewRequest is built with
         decided_by="llm_provisional"
       resume_review_state(...) runs the existing in-process resume path
       ledger records auto_decision_committed
  -> when BLOCK_*:
       existing active_interrupt / sqlite checkpoint flow takes over
       resume CLI requires decided_by="human" by default
       BLOCK_WITH_TIMEOUT also writes blocked_with_timeout into ledger
         so a future operator knows the wait was bounded
```

The graph node responsible for "request_review" stays where it is. The
new policy module is consulted from inside that node. No top-level
routing changes.

## Run-Config Surface

```json
{
  "review_policy": {
    "mode": "auto",
    "max_blocked_seconds": 86400,
    "stages": {
      "hic_scaffolding": { "mode": "strict" }
    }
  }
}
```

- `mode`: `"strict"` (default) or `"auto"`.
- `max_blocked_seconds`: integer. Strict-mode default is no timeout.
  Auto-mode default is small (e.g. 0) so the policy short-circuits
  immediately to `AUTO_DECIDE_NOW` for auto-decidable checkpoints.
- `stages.<stage>`: optional per-stage overrides. Same shape.

The top-level mode is sufficient for most users. Per-stage overrides
exist so `hic_scaffolding` can be forced strict even in an otherwise
auto-mode run, because Hi-C contact-map judgment is one of the
not-auto-decidable gates.

## auto_decidable: false Checkpoints

Recorded in the review request payload, sourced from the stage / method
descriptor. Initial set:

- `hic_scaffolding` contact-map review — misjoin signals are visual.
- `assembly_review` decisions when contamination signals are present in
  the inputs (e.g. when contamination check ever lands; today this
  reduces to "whenever a contamination check artifact is registered").
- Any `select_method` style decision where the LLM has no metric basis
  to choose between near-equivalent candidates.

The list is expected to grow as more methods land. The descriptor is
the source of truth; do not hardcode the list in the policy module.

## Tasks

- [ ] Add `review_policy` block to `app/schemas/run_config.py` with
  validation (mode enum, non-negative timeout, stage override schema).
- [ ] Add `decided_by` and `rationale_artifact_ref` fields to
  `ResumeReviewRequest`. Make `rationale_artifact_ref` required when
  `decided_by != "human"`.
- [ ] Add `auto_decidable: bool` to the review request payload contract
  in `app/review/base.py`. Default `true` until a stage opts out.
- [ ] Add an `app/review/policy.py` module with a single function
  `evaluate(stage, request, run_config) -> PolicyOutcome`. Pure
  function; no I/O; no LLM calls.
- [ ] Add an `app/llm/prompt_bundles/prompts/review/<stage>/recommend.md`
  template per stage that has a review gate. The prompt receives the
  review request payload and returns JSON that maps onto a
  `ResumeReviewRequest`.
- [ ] Add an `app/review/recommender.py` that calls the LLM, validates
  the output as a `ResumeReviewRequest`, and writes the recommendation
  artifact (`<stage>_review_recommendation.json`).
- [ ] Wire policy + recommender into the existing `request_review`
  node for `assembly_review` and `hic_scaffolding`. The node consults
  the policy first, then the recommender if the policy says
  `AUTO_DECIDE_NOW`, then either commits the synthetic resume request
  or sets `active_interrupt` exactly as today.
- [ ] Add `--max-blocked-seconds` to `gene-agent resume` (the task 02
  future-work item). The flag forwards directly into
  `resume_review_state(blocked_timeout_seconds=...)`.
- [ ] Extend `gene-agent inspect` to print
  `decisions_by_origin: { human: N, llm_provisional: M, auto_rule: K }`
  and to accept `--filter decided_by=llm_provisional` to list only
  those decisions.
- [ ] Add a `final_report` artifact at workflow termination
  (`gene_annotation` / `function_annotation` final commit) that lists
  every recorded decision in the session, grouped by `decided_by`. The
  list of `BLOCK_PENDING_CONFIRM` checkpoints comes first.
- [ ] Tests:
  - run-config schema accepts strict / auto / per-stage overrides;
    rejects unknown modes.
  - policy returns `AUTO_DECIDE_NOW` for an auto-decidable
    `assembly_review` request under `mode: auto`.
  - policy returns `BLOCK_PENDING_CONFIRM` for an `auto_decidable:
    false` request under `mode: auto`.
  - synthetic `ResumeReviewRequest` produced by the recommender is
    accepted by `resume_review_state(...)`.
  - LLM rationale artifact is registered and reachable from `inspect`.
  - `--max-blocked-seconds` is forwarded into the existing in-process
    timeout path.
  - end-to-end auto-mode run on the existing fake-binary fixtures
    advances `assembly_review` to a downstream stage without an
    operator typing `gene-agent resume`.
- [ ] Update `docs/handoff.md` with one example auto-mode session id
  and the location of its final report artifact.

## Implementation Notes

- `app/schemas/run_config.py` now accepts `review_policy` with strict/auto mode, non-negative `max_blocked_seconds`, and per-stage overrides for `assembly_review` and `hic_scaffolding`. It also accepts `function_annotation.database_provenance` so smoke fixtures can seed the downstream provenance the workflow needs to reach successful terminal commit.
- `app/review/policy.py` is pure and evaluates only an effective per-stage policy plus the review payload; biological auto-decidability remains stage-filled and outside the policy module.
- `ReviewPayload` carries `auto_decidable`, and `ResumeReviewRequest` carries `decided_by` plus `rationale_artifact_ref` for non-human decisions.
- **`llm_provisional` is policy-gated auto-review, not a bypass.** When `policy.evaluate(...)` returns `AUTO_DECIDE_NOW`, the recommender produces a `ResumeReviewRequest` with `decided_by="llm_provisional"`, the review module synthesizes the resume, calls the existing `resume_review_state(...)` pipeline, and the graph advances. The LLM never writes ledger events directly; review code records `auto_decision_committed` with the rationale artifact ref. The human gate is preserved structurally — the policy decides whether to invoke it; the LLM recommends a decision within `decisions_allowed` only when policy says auto.
- `_assembly_review_auto_decidable` requires (a) at least one candidate with a `candidate_id`, (b) at least one eval signal in `{quast, busco, merqury, mapping_coverage}` with `status="available"` and non-empty `metric_refs`, and (c) no `contamination_check` signal in `available` status with metric_refs. A pending contamination_check signal is not evidence and does not flip the gate. With all signals pending, `auto_decidable` is `False` and the workflow blocks for human confirmation even in `auto` mode.
- The recommender writes its rationale artifact with `model`, `prompt_path`, `decisions_allowed`, the full `review_request`, the parsed `recommendation` JSON, and the raw `rationale_text`. Every auto decision is reproducible from this artifact plus the review request.
- `assembly_review` attempts an LLM provisional recommendation only when `outcome.action == AUTO_DECIDE_NOW`. Recommendation/validation failures (invalid JSON, disallowed decision, LLM exception, synthetic resume validation failure) fail safe back to `blocked_human` and record `auto_recommendation_failed` with `error_type` and `error` fields. The review request artifact remains available for a human operator.
- `hic_scaffolding` writes a review request with `auto_decidable=False`; auto mode records pending human confirmation and never calls the recommender.
- `review_requested` and `review_decision` ledger events are self-describing with request refs, policy/provenance fields, and `decided_by` where applicable.
- `gene-agent inspect` reports decision origins and can filter decisions by `decided_by`; current blocked decision requirements include auto-decidable/pending-confirmation policy context.
- Final review decision reports are written on the normal successful `function_annotation` terminal path at `artifacts/final_report/<session>/review_decision_report.json`. Decision-record projection and origin counting are shared between `inspect` and the final report via `app/review/records.py`.
- **Known limitation — stage-level resolution**: pending-confirmation tracking in the final report and `inspect_session` resolves a `review_requested` event by looking up any `review_decision` for the same stage. If a future code path emits multiple `review_requested` events for the same stage in one session (retry or multi-round review), a single decision will mark all of them resolved. No committed code path does this today; introducing one requires switching to per-request binding via `review_request_id` or the request artifact ref.
- Strict-mode behavior compatibility means routing/blocking stays compatible; deterministic existing-assembly rule decisions are now explicitly labeled `decided_by="auto_rule"`.

## Acceptance

- A user can pick `auto` and finish a small fake-binary mainline run
  without typing `gene-agent resume`, and inspect every decision after
  the fact.
- A user can pick `strict` with `max_blocked_seconds`, leave for a few
  hours, and have the workflow either commit on their decision file or
  fall back to a recorded LLM provisional decision when the timeout
  fires (when policy.mode allows it).
- Every decision in the ledger carries `decided_by`. Filtering by
  `decided_by="llm_provisional"` reproduces the same list every run for
  the same inputs.
- `auto_decidable: false` gates always block in `auto` mode and are
  always at the top of the final report.
- Existing strict-mode runs (no `review_policy` block in run-config)
  behave identically to today.
- The LLM is never the source of a ledger event by itself; it produces
  a rationale artifact, and the review module writes the ledger event
  with the artifact ref.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent \
  python -m unittest tests.test_review_policy tests.test_review_recommender \
  tests.test_review_resume tests.test_entrypoints -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent \
  python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent \
  python -m compileall app tests
git diff --check
```

Server-side smokes (two narrow scopes):

```bash
# Scope A — auto_rule + final_report on the deterministic existing-assembly path.
# Validates: auto_rule decision recorded, function_annotation terminal commit,
# review_decision_report.json artifact written.
# Does NOT validate LLM provisional review (deterministic_safe bypasses the gate).
bash scripts/server_phase5_auto_review_smoke.sh \
  --manifest    /abs/path/to/data/phase5_auto_review/manifest.yaml \
  --run-config  /abs/path/to/data/phase5_auto_review/run_config.json \
  --session-id  phase5-auto-review-YYYYMMDD-a \
  --data-dir    /abs/path/to/data/phase5_auto_review/out

# Scope B — LLM provisional review at the assembly_review human gate.
# Validates: auto_decision_committed event with decided_by=llm_provisional,
# assembly_review.review_recommendation artifact, run blocks at hic_scaffolding
# (Hi-C is auto_decidable=false in auto mode).
# Requires: GENE_AGENT_LLM_API_KEY exported, Flye + QUAST in env_geno_exec.
bash scripts/server_phase5_llm_review_smoke.sh \
  --manifest    /abs/path/to/data/phase5_llm_review/manifest.yaml \
  --run-config  /abs/path/to/data/phase5_llm_review/run_config.json \
  --session-id  phase5-llm-review-YYYYMMDD-a \
  --data-dir    /abs/path/to/data/phase5_llm_review/out
```

The `phase5_auto_review` template `run_config.json` seeds
`function_annotation.database_provenance` with a placeholder
`__REPLACE_WITH_PROTEINS_FASTA_REF__`. The smoke script substitutes
`<data_dir>/work/gene_annotation/<session_id>/proteins.fa` and the post-run
check fails loudly if `gene_annotation` ever changes that output convention.

Both scripts are prepared but server execution is pending. HPC acceptance for
task 04 requires running each on the server and recording the session id,
exit code, and the relevant artifact refs in `docs/handoff.md`.

## Open Questions Left For Implementation

- Whether `mode: auto` on a checkpoint with no descriptor entry should
  default to `auto_decidable: true` or `false`. Recommendation: default
  `true` (most checkpoints are recoverable from the artifact trail);
  the descriptor opts specific gates *out*.
- Whether the LLM recommendation should be allowed to choose
  `stop_human_review` (i.e. recommend that a human take over). Probably
  yes — it is an honest recommendation in low-confidence cases and
  makes the system fail safer. The CLI then surfaces this exactly like
  a `BLOCK_PENDING_CONFIRM` outcome.
- Whether `final_report` belongs in `app/reports/` or as a stage-level
  commit artifact. `app/reports/` is currently empty; this task is a
  reasonable first user.
