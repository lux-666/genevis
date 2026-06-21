# Hi-C TUI clarification design

## Goal

Keep Juicer + 3D-DNA and YaHS as the default Hi-C candidate set without
silently substituting one method for the other. When the operator intends to
execute a candidate but its method-specific inputs are incomplete, the TUI
collects the missing information before execution.

## Behavior

- The plan continues to recommend both executable Hi-C candidates.
- The TUI keeps the existing method-selection step and asks for each selected
  method's execution inputs.
- Juicer + 3D-DNA asks for the mode, chromosome count, and then either a
  `merged_nodups` path or the raw-read prerequisites.
- YaHS asks for / validates a materialized Hi-C alignment when the operator
  selects it for local execution.
- A permissive ``HiCScaffoldingPlanCandidate`` represents each selected method
  during planning. It derives ``missing_fields`` and either
  ``needs_clarification`` or ``ready_to_execute`` without validating a complete
  run configuration.
- ``RunConfig`` is the strict execution schema. Input assets live at the
  ``hic_scaffolding`` block's top level (for example ``merged_nodups_ref`` and
  ``hic_alignment_ref``), while ``juicer_3ddna_parameters`` contains tool
  options. Only ready candidates are emitted into ``local_exec``.
- A non-interactive plan does not remove Juicer or silently fall back to YaHS
  merely because Juicer parameters are absent. It remains a plan requiring
  clarification; local execution is enabled only for fully specified methods.
- Routing must never launch YaHS solely because it was a default candidate when
  its required alignment is unavailable. The operator can still review and
  accept any successfully executed candidate.

## Verification

- Unit-test the planner payload for incomplete Juicer parameters: no fallback
  method substitution and no invalid run config.
- Unit-test the TUI prompt flow for Juicer and YaHS method-specific fields.
- Unit-test a default dual-candidate state where only Juicer is executable: it
  must preserve the successful Juicer candidate and request clarification for
  YaHS rather than treating YaHS as a failed automatic fallback.
- Run the targeted entrypoint/Hi-C tests and the complete test suite.
