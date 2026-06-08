# 05 TUI Initial Ask Plan Preview

Task status: done. Superseded by Phase 7 task 10: the `auto_resolve_plan` /
`_confirm_auto_resolve` "Plan Preview" accept path described below was replaced
by the unified `Plan Draft` → `Accept this plan?` flow in
`docs/phases/phase7/10_natural_language_draft_plan_approval.md`. The
`_render_plan_summary` preview renderer documented here is retained and reused.


## Goal

Make the first `gene-agent tui` ask-planning screen show a richer plan preview
before the operator answers the initial plan question. The preview should feel
like the final `Run Plan` confirmation screen: explicit session context, input
refs, stage order, stage roles, selected or suggested methods, execution support,
shared parameters when known, method parameters when known, warnings, and
run-config preview errors.

The operator should see enough detail early to decide whether the auto-detected
or suggested plan is reasonable, instead of only seeing a short stage/method
summary and discovering the full shape at the final confirmation step.

## Current Problem

The final confirmation renderer already prints a detailed `Run Plan` summary.
The earlier auto-detected plan prompt is much shorter: it prints selected stages,
dependency stages, and a flattened method list before asking whether to accept.
That early display is not enough when the TUI has already inferred a multi-stage
workflow from natural language and materialized manifest data.

This task should close that UX gap without changing planning semantics.

## Scope

- Add an initial plan preview for the TUI/interactive ask flow before accepting
  an auto-resolved plan.
- Reuse the same presentation contract as the final `Run Plan` renderer as much
  as possible.
- Show known values in the same sections as the final confirmation display:
  session, data dir, manifest, inputs, stages, methods, shared parameters,
  method parameters, warnings, and run-config preview errors.
- Show unknown or not-yet-selected values as `pending` or omit sections that are
  genuinely unavailable; do not fabricate defaults that the planner has not
  resolved.
- Keep the existing final confirmation step. The early preview is advisory and
  does not replace the final operator confirmation.
- Preserve plain-text fallback behavior and questionary behavior.

## Non-Goals

- Do not change stage selection, method selection, parameter resolution, or
  auto-resolve rules.
- Do not execute any workflow work during preview rendering.
- Do not add a web UI or curses-style full-screen TUI.
- Do not require a live terminal for tests.
- Do not print large manifests, FASTA/FASTQ contents, command logs, or full
  descriptor payloads.

## Design Direction

Prefer extracting a shared plan-rendering helper that can render both:

- final confirmation mode, labelled `Run Plan`;
- initial preview mode, labelled `Plan Preview` or similar.

The renderer should accept the available ask-plan summary payload and a display
mode. In preview mode, unresolved sections should remain visibly incomplete
rather than hidden behind a misleading final-looking display. The final confirm
mode should keep the existing `Run Plan` output unless a small formatting change
is needed to share the helper.

If the initial auto-resolve prompt has only an `AskPlanState` rather than a full
clarification summary, add a small summary builder rather than duplicating
rendering logic. That builder should use the same fields later produced for
confirmation when they are already derivable.

## Operator Experience Requirements

For an auto-resolved raw-read assembly request, the first accept/reject prompt
should show at least:

- session id, data dir, manifest ref, and input paths when available;
- selected target stages and dependency stages;
- resolved execution order;
- selected methods, including local/deferred tags when known;
- shared parameters already inferred or supplied;
- method parameters already supplied or still pending;
- warnings or run-config preview errors.

For a plan that cannot be auto-resolved, the normal stage-selection flow should
continue to show available stages and recommended stages, but it may also show a
compact preview of known request context if that can be done without adding
noise.

## Data Consistency Requirements

- Preview rendering must be read-only.
- Preview rendering must not save a different ask plan or change confirmation
  state.
- The early preview must not claim a method is executable unless the method
  registry says it is executable.
- The early preview must not hide later required parameter prompts.
- The final confirmation remains the last authoritative plan display before
  execution.

## Tasks

- [x] Identify the current final `Run Plan` renderer and early auto-resolve
  prompt.
- [x] Extract or introduce a shared plan preview renderer.
- [x] Add a summary builder for early preview mode when no clarification summary
  is available yet.
- [x] Render the detailed preview before the initial auto-resolve accept prompt.
- [x] Preserve the final confirmation rendering and semantics.
- [x] Add tests for preview output with stages, dependencies, methods, shared
  parameters, method parameters, warnings, and pending values.
- [x] Add a TUI/ask interaction test proving the preview appears before the
  initial accept prompt and execution is not started by rendering.
- [x] Update operator docs or handoff notes if the printed text changes in a way
  users should know about.

## Acceptance

This task is complete only when:

- the initial TUI/ask auto-resolve prompt shows a detailed plan preview;
- the preview uses the same stage/method/support vocabulary as the final
  confirmation display;
- unknown values are clearly marked or omitted without being fabricated;
- final `Run Plan` confirmation still appears before execution;
- tests cover both the early preview and final confirmation paths.

## Implementation Notes

- Reused the existing `_build_run_plan_summary` builder unchanged; only renamed
  it to public `build_run_plan_summary` (`app/entrypoints/ask_planner.py`). It
  already returns the full read-only summary (session, data dir, manifest,
  source paths, stage roles, methods, exec status, shared/method params,
  warnings, run-config preview error) from an `AskPlanState`, so no separate
  preview summary builder was needed.
- Extracted `_render_plan_summary(summary, *, title, preview=False)` from
  `_render_confirm` (`app/entrypoints/ask_interactive.py`). `_render_confirm`
  now calls it with `title="Run Plan"`; the final-confirm output is byte-for-byte
  unchanged. Pulled the local/deferred tag logic into `_method_tag`.
- Preview mode (`preview=True`) is only additive: it iterates the full execution
  order and prints `<stage>: pending` for stages with no selected method yet,
  and `Shared parameters: pending` / `Parameters: pending` when those are unset,
  plus a trailing "Advisory preview" line. `[local]`/`[deferred]` tags come
  straight from the method registry via `method_execution_status`, so a method
  is never labelled executable unless the registry says so.
- `_confirm_auto_resolve` now renders `build_run_plan_summary(plan)` with
  `title="Plan Preview", preview=True` before the accept prompt. Rendering is
  read-only — no plan is saved and execution is not started; the existing final
  `Run Plan` confirmation step is untouched.
- Did not modify task 04's run-trace files (`run.py`, `test_run_trace.py`, the
  task 04 doc).
- The interactive auto-resolve output changed materially (it now prints a full
  plan preview), so `docs/handoff.md` Phase 6 Ask Acceptance section records the
  new operator-facing text.

## Handoff Record Requirements

After implementation or validation, update `docs/handoff.md` if this becomes
the next recommended operator-facing task or if a real TUI transcript reveals
remaining UX gaps.
