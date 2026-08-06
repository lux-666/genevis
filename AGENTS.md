# Repository Development Rules

- The repository-local Sol-Luna skill and workflow are temporarily disabled. Do not invoke them or delegate through their role-specific workflow unless the user explicitly asks to restore them.
- Preserve the disabled files under `.agents/disabled-skills/sol-luna/`.
- Never report a test as passed merely because its command started. Require successful completion and expected assertions or evidence.
- Support material conclusions with file paths, symbols, commands, configuration keys, diffs, or test output.
- Keep changes minimal. Do not refactor unrelated code, add unrequested architecture, or introduce production dependencies opportunistically.
