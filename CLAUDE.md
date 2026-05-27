# CLAUDE.md

## Coding Principles

- State assumptions before implementing. If multiple interpretations exist, present them.
- Minimum code that solves the problem. No speculative features, abstractions, or error handling for impossible scenarios.
- Surgical changes only: every changed line must trace to the request. Don't improve adjacent code. Remove only orphans YOUR changes created.
- For multi-step tasks, state a brief plan with verification steps before coding.
