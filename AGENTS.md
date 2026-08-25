# Repository Development Rules

- The repository-local Sol-Luna skill and workflow are temporarily disabled. Do not invoke them or delegate through their role-specific workflow unless the user explicitly asks to restore them.
- Preserve the disabled files under `.agents/disabled-skills/sol-luna/`.
- Never report a test as passed merely because its command started. Require successful completion and expected assertions or evidence.
- Support material conclusions with file paths, symbols, commands, configuration keys, diffs, or test output.
- For duplicate-code work, run the read-only inventory workflow in
  `docs/architecture/reference/governance.md` before editing. Classify every
  candidate as `exact clone`, `semantic duplication`,
  `duplicate representation`, `dead surface`, or `intentional symmetry`, and
  list all production call sites.
- Prefer deleting one source of truth over adding an abstraction. Before adding
  shared code, state the invariant that both callers share and the changes that
  must move together versus independently. If this proof is unclear, leave the
  repetition in place and record it as a candidate instead.
- Keep changes minimal. Do not refactor unrelated code, add unrequested architecture, or introduce production dependencies opportunistically.
