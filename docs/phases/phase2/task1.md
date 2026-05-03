# Task 1: Move Phase 1 Leftovers Into Phase 2 Decisions

Status: planned.

## Goal

Keep Phase 1 closed while preserving the unresolved design decisions that matter
for a real Phase 2 method slice.

## Required Behavior

- [ ] Keep Phase 1 task checkboxes unchanged unless a regression appears.
- [ ] Track review/resume protocol under Phase 2 gates, not Phase 1 inventory.
- [ ] Track requested-stage routing under Phase 2 entrypoint semantics.
- [ ] Track normalized manifest artifacts under Phase 2 artifact contracts.
- [ ] Track registry event metadata under Phase 2 ledger/artifact alignment.

## Human Help Triggers

- [ ] If a coding assistant needs to define what a human approval payload looks
  like.
- [ ] If a coding assistant needs to decide whether `--stages` means partial
  graph execution, direct stage entry, or dependency-aware replay.
- [ ] If a coding assistant needs to decide what audit guarantees normalized
  manifests must provide.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。

## Files

- `docs/phases/phase1/phase1.md`
- `docs/phases/phase2/phase2.md`
- `docs/phases/phase2/task*.md`

## Verification

```bash
git diff --check
```
