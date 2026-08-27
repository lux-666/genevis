# Parameter Recommendation

This operator guide explains what happens when Gene Agent asks for method
parameters and how to review the result. The exact model DTO, JSON response
shape, schema versions, persistence graph, and retry invariants are maintained
only in the
[Parameter Recommendation Contract](../architecture/reference/parameter-recommendation.md).
The subsystem explanation is
[Parameter Recommendation](../architecture/topics/parameter-recommendation.md).

## Where recommendations appear

Assembly, AutoHiC, and T2T may request parameters after the execution method is
known. A valid recommendation is persisted before any execution attempt exists,
then exposed through a `parameter_approval` gate.

The model proposes parameters; it does not approve them, select a different
method, route the workflow, or start a process. A malformed/unavailable model
response fails closed and does not silently fall back to catalog defaults.

## What the operator reviews

At the gate, check:

- the selected stage, method, and workflow occurrence;
- the exact sparse parameter mapping proposed for this attempt;
- the optional model summary and the persisted biological basis;
- whether the values fit the intended reads, species, genome target, resources,
  and retry context;
- whether omitted values should remain provided facts, executor-owned defaults,
  or tool defaults.

The proposal can be empty. Omitted keys are not automatically inherited from a
previous attempt, and the application does not repair an unusual value behind
the operator's back. Command construction may still reject an approved value
that cannot be represented safely by the selected tool.

## Approve or reject

During an attached run, follow the displayed gate prompt. The frozen review
policy determines whether that gate waits for a human or has an eligible
foreground countdown. Any automatic parameter approval, when allowed, applies
only to the exact persisted recommendation bound to the active request; it does
not regenerate or edit the mapping.

If the process is interrupted, inspect and recover it with the commands in
[Run and Recovery](run-and-recovery.md). A later `review` or `resume` is a
manual recovery action and does not reconstruct an expired foreground timer.

Rejecting a recommendation creates no execution attempt. A later retry, where
the stage supports one, produces a new independently reviewable recommendation
whose model context may include bounded evidence from earlier attempts.

## Audit evidence

Recommendation evidence is stored under the active workflow step's artifact
tree. It includes the validated input/basis snapshots, exact model request and
response, parsed recommendation, parse result, and integrity manifest. Approval
is bound to that persisted root before execution parameters are frozen.

Use the session summary or artifact index to locate the current recommendation;
do not edit stored recommendation files in place. Hash, identity, stage/method,
and predecessor mismatches fail closed.

Exact filenames, schema versions, accepted response fields, source precedence,
T2T motif-resolution rules, and retry-chain invariants are deliberately not
duplicated here; use the reference contract when debugging or changing them.

## Operator-owned inputs

Species identity belongs only to manifest `user_metadata.target_species` as an
NCBI Latin scientific name. RunConfig does not repeat
`shared_parameters.species_facts.scientific_name`; runtime projects the manifest
identity into canonical recommendation facts. RunConfig may independently
supply remaining facts such as `shared_parameters.species_facts.ploidy`; that
value drives GenomeScope and Hifiasm's `--n-hap` while the scientific name still
comes from the manifest. Explicit Hi-C settings, retry policy, and T2T read
declarations remain in their schema-owned authored inputs.
Start from the schema-valid files in [`../examples/`](../examples/) and validate
them through the normal `gene-agent run` boundary. Recommendations and example
values must never silently become additional RunConfig validation rules.
