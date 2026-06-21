# Hi-C 3D-DNA dual-input execution design

## Goal

Make `hic_scaffolding.candidate.juicer_3ddna_juicebox` the default executable
Hi-C scaffolding method.  It must use the project's existing attempt lifecycle:
freeze a reproducible shell command, run it through the shared runner, record
the ledger, register artifacts, and stop for Juicebox-backed human review.

The implementation must not depend on a site-specific `hic` wrapper command.
Queue, scheduler, and resource policy remain execution-environment concerns.

## Inputs and modes

The method has two input modes:

1. `raw_reads`: paired registered Hi-C FASTQ files are processed by Juicer to
   produce `merged_nodups.txt`, then passed to 3D-DNA.
2. `merged_nodups`: a registered, materialized `merged_nodups.txt` bypasses
   Juicer and is passed directly to 3D-DNA.

Both modes require the final assembly selected by `assembly_review`.  The
method validates the selected mode and required parameters before freezing an
attempt.  Juicer-specific paths/options (for example restriction-site metadata
and threads) are explicit runtime parameters, not hard-coded cluster commands.
The 3D-DNA chromosome count is also explicit (`chr_num`; e.g. 22 for 2n=44).

## Lifecycle

`hic_scaffolding` gains a 3D-DNA method adapter parallel to Flye's method
adapter pattern:

```text
resolve inputs
  -> plan candidate
  -> freeze_juicer_3ddna_attempt
  -> run_frozen_attempt
  -> collect_juicer_3ddna_artifacts
  -> human Juicebox review gate
```

The frozen script performs, as applicable:

```text
Juicer raw reads -> merged_nodups.txt
run-asm-pipeline.sh [3D-DNA options] assembly.fasta merged_nodups.txt
```

It copies a validated scaffold FASTA into the managed artifacts directory.
The runner records the exact script and environment through `FrozenAttemptSpec`.
Collection registers the scaffold assembly, AGP/contact-map review outputs when
present, stdout/stderr, and an executable candidate/review bundle.  Failed
attempts retain the pre-Hi-C assembly as the downstream fallback and produce a
failed ledger event.

## Selection and compatibility

The planner defaults to `hic_scaffolding.candidate.juicer_3ddna_juicebox`.
YaHS remains descriptor-compatible for existing sessions but is no longer the
default executable route.  Its existing behavior is not changed as part of
this focused task.

## Tests

Tests use fake `juicer.sh` and `run-asm-pipeline.sh` binaries to verify:

- raw-read mode runs Juicer then 3D-DNA and registers the expected artifacts;
- precomputed `merged_nodups.txt` bypasses Juicer;
- missing mode-specific parameters block before execution;
- the frozen script contains the validated commands and parameters;
- failures create ledger failure records and preserve the pre-Hi-C fallback;
- the planner chooses 3D-DNA by default.
