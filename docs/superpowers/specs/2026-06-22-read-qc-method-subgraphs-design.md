# Read QC method subgraphs

## Goal

Refactor the read QC stage so its executable methods are represented as
method-specific LangGraph subgraphs, following the assembly-stage pattern,
without changing the stage's externally observable state, artifacts, or
routing outcomes.

## Scope

Create `app/stages/read_qc/method_application/` with three adapters:

- `seqkit_stats.py` for `read_qc.long_read_stats.seqkit_stats`.
- `seqkit_length_filter.py` for `read_qc.length_filter.seqkit_seq`.
- `make_fofn.py` for `read_qc.make_fofn.local`.

The read QC stage builder remains responsible for input resolution, method
planning and selection, terminal failure handling, and commit. It invokes the
three method subgraphs rather than registering their implementation nodes
directly.

## Architecture

`method_application/lifecycle.py` supplies a small read-QC-specific shared
lifecycle for executable methods: `freeze -> run -> collect -> done`. It owns
the runner invocation and the status-gated edges only. Method adapters own
their command construction, expected outputs, artifact parsing/registration,
and read-QC state updates.

SeqKit stats and SeqKit length filtering use this lifecycle. The FOFN adapter
is intentionally a one-node subgraph because it only materializes and records
a local file; forcing it through the command lifecycle would add indirection
without a useful shared concern.

## Data flow

```text
resolve raw reads -> plan stats -> seqkit-stats subgraph -> plan filter
    -> seqkit-length-filter subgraph -> make-fofn subgraph -> commit
```

Stats planning may bypass its subgraph when local execution is disabled.
Length-filter planning may bypass execution for unapproved, unsupported, or
absent filter requests. Both cases still enter the FOFN subgraph as today.

## Compatibility and error handling

Existing `stage_outputs.read_qc` keys, statuses, artifact types, ledger
events, generated paths, and failure/blocked behavior are retained. Each
method subgraph returns to the stage graph with the same status values that
the current routing functions consume. No global execution framework is
introduced; the shared lifecycle is confined to read QC.

## Verification

Retain the current read-QC end-to-end tests and add focused tests that ensure
the stage builder registers the three subgraphs and that success, skip, and
failure paths preserve their present routing and output contracts.
