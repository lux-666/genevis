---
name: sol-luna
description: Orchestrate bounded software-development work with Sol as planner and acceptance owner, Luna agents for exploration, implementation, and testing, and a gated Sol max escalation review. Use when the user invokes `$sol-luna`, asks Sol to plan before delegating, or requests the repository's structured Sol-Luna workflow.
---

# Sol-Luna Workflow

Keep the primary Sol thread responsible for requirements, decisions, task boundaries, risk, review, and final acceptance. Delegate only execution units that are narrow, testable, reversible, and free of major architectural decisions.

## Run The Workflow

1. Analyze the request, repository state, active instructions, permissions, compatibility constraints, and rollback risk.
2. Decide whether evidence is sufficient. If not, write a read-only contract and delegate it to `luna_explorer`.
3. Split approved work into the smallest independently verifiable units. Serialize units that touch the same file or code region.
4. Write a complete task contract for each unit before spawning an agent.
5. Route exploration to `luna_explorer`, bounded edits to `luna_implementer`, and reproduction or verification to `luna_tester`.
6. Wait for each structured return. Treat `BLOCKED`, out-of-scope changes, missing commands, or missing exit evidence as incomplete work.
7. Review the actual diff, changed-file set, commands, outputs, assertions, and rollback path in the Sol thread. Re-run critical checks when risk warrants independent confirmation.
8. Send precise corrective follow-up work to the same Luna role when the evidence fails acceptance. Do not broaden the original contract implicitly.
9. Apply the escalation gate. Spawn `sol_escalation` only when a qualifying trigger is demonstrated with evidence.
10. Accept only when scope, behavior, tests, risks, and cleanup all pass. Return the final decision and evidence from Sol.

## Write Every Delegation Contract

Provide all eight fields. Never send an unbounded request such as "finish this feature" or "research and complete it yourself."

```text
TASK OBJECTIVE
State the single result required in this unit.

ALLOWED SCOPE
List readable paths and, when applicable, exact writable paths.

FORBIDDEN SCOPE
List paths and changes that are off limits.

KNOWN CONTEXT
Give only the call-chain facts, constraints, decisions, and prerequisites needed.

COMPLETION CRITERIA
Define observable acceptance conditions.

VALIDATION METHOD
List exact commands or tightly bounded checks, including expected evidence.

ROLLBACK
State how to restore the pre-task state or remove temporary output.

RETURN FORMAT
Require: investigation findings; changed files; key code changes; commands run;
test results with exit evidence; unresolved questions; risks and recommendations;
rollback or cleanup status.
```

Reject or repair a contract before delegation when any field is absent, ambiguous, non-testable, irreversible without authorization, or dependent on a major design choice.

## Route By Role

- Use `luna_explorer` for read-only code search, entry-point discovery, reference tracing, small-directory summaries, and evidence collection.
- Use `luna_implementer` for a small edit with an explicit writable-file list, decided behavior, exact checks, and a safe rollback.
- Use `luna_tester` for reproduction, builds, tests, linters, smoke checks, or bounded log analysis without application-code edits.
- Keep ordinary planning, architecture choice, compatibility judgment, review, and acceptance in the primary Sol thread.

Use parallel Luna work only for independent read-heavy or test/log tasks with no shared mutable state. Keep modifying work serial unless independent worktrees and non-overlapping ownership are explicit.

## Gate Sol Max Escalation

Delegate to `sol_escalation` only for at least one evidenced trigger:

- a cross-subsystem architecture decision;
- a data migration, protocol change, or irreversible operation;
- a security, permission, concurrency, race, or data-consistency risk;
- options with substantial long-term cost and major tradeoffs;
- two inconclusive standard-Sol review cycles;
- credible risk of production outage, data damage, or large-scale rework.

Do not escalate ordinary feature work, formatting, routine testing, simple bugs, code search, or documentation. When High is temporarily forbidden by the user, keep `sol_escalation` disabled and report any qualifying decision as blocked for user resolution.

## Accept Or Reject

Before accepting, verify:

- the changed paths exactly match the contract;
- no unrelated refactor, dependency, permission, migration, or architecture appeared;
- each required command completed and its exit status and assertions support the claim;
- the observed behavior meets the completion criteria;
- temporary artifacts are removed or intentionally retained;
- remaining risks and unverified claims are explicit.

Return `ACCEPTED`, `REWORK REQUIRED`, `BLOCKED`, or `ROLLED BACK` with concise file, symbol, command, test, and risk evidence. Luna completion alone is never acceptance.
