# Sol-Luna Development Rules

- Keep requirements, decisions, risk calls, review findings, and final acceptance in the primary Sol thread.
- Delegate bulky search output, test logs, and intermediate exploration to the appropriate Luna thread when the task contract is bounded.
- Run read-heavy, independent exploration, retrieval, and log analysis in parallel only when agents do not share mutable state.
- Serialize tasks that may modify the same file or code region. Without independent worktrees, never let multiple agents edit the same area concurrently.
- Treat a Luna completion as evidence, not acceptance. Sol must review the diff, scope, commands, and test results before accepting it.
- Never report a test as passed merely because its command started. Require successful completion and expected assertions or evidence.
- Support material conclusions with file paths, symbols, commands, configuration keys, diffs, or test output.
- Keep changes minimal. Do not refactor unrelated code, add unrequested architecture, or introduce production dependencies opportunistically.
- Route routine exploration, implementation, testing, formatting, simple bugs, and documentation to standard Sol/Luna; do not use `sol_escalation` for them.
- Use `sol_escalation` only for the qualifying high-risk cases defined in its project agent file and in the `$sol-luna` workflow.
