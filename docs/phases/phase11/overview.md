# Phase 11: Expand Parameter Attempts Across Assembly Methods

**Phase**: 11
**Status**: Task 1 complete
**Created**: 2026-07-07
**Updated**: 2026-07-08

## Goal

Carry the Phase10 typed parameter-attempt contract beyond the initial
hifiasm/hifiasm_ont slice so every executable assembly method can use the same
catalog, command artifact, lifecycle update, and retry-gate behavior.

Phase11 starts with Flye and NextDenovo because they were still using
method-specific run-configuration paths while hifiasm already used cataloged
parameter attempts.

## Current Scope

Implemented in Task 1:

- Flye has a method parameter catalog and descriptor `parameter_catalog` wiring.
- NextDenovo has a method parameter catalog and descriptor `parameter_catalog`
  wiring.
- Flye freeze builds `parameter_sources` through the shared catalog-driven
  builder, writes an attempt-scoped `command.json`, and records typed
  `parameter_attempts` lifecycle state.
- NextDenovo freeze builds `parameter_sources` through the shared catalog-driven
  builder, writes an attempt-scoped `command.json`, and records typed
  `parameter_attempts` lifecycle state.
- Shared method execution lifecycle now updates the current parameter attempt
  for success and failure instead of skipping Flye/NextDenovo.
- After candidate selection, Flye and NextDenovo now enter the same
  `recommend_assembly_parameters` path as hifiasm/hifiasm_ont before freeze/run.
- Model unavailable is not silently bypassed. By default, recommendation
  failure stops before local execution; `allow_model_unavailable_defaults` is an
  explicit test/operator fallback that lets catalog defaults stand.
- Assembly retry routing now sends method execution failures through the
  parameter retry gate consistently.
- Tests cover catalog presence for all executable assembly methods and typed
  attempt lifecycle records for Flye and NextDenovo local execution.

## Operator Contract

For real runs, all executable assembly methods require a successful typed
parameter recommendation attempt before freeze/run. Supplying
`{method}_parameters` still provides L0 execution inputs, but it no longer skips
the recommender for Flye or NextDenovo.

If the LLM or assembly decision backend is unavailable, the assembly stage stops
with a validation-failed parameter attempt before launching local execution.
Operators should fix LLM configuration and resume/retry the session from the
assembly checkpoint. A future TUI integration should make that resume action
explicit; Phase11 only standardizes the graph contract.

## Task Index

- [Task 1: Flye and NextDenovo Typed Parameter Attempts](task_01_flye_nextdenovo_parameter_attempts.md) - Completed
