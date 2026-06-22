# Assembly Dedup Default-Auto Design

## Goal

Run conservative `purge_dups` automatically after a materialized assembly,
without `local_exec` or user-supplied cutoff paths, while retaining the raw
assembly for comparison in assembly review.

## Data Flow

`assembly` provides the materialized raw candidate and its long-read input.
`assembly_dedup` derives read-mapping coverage, `PB.stat`, calcuts cutoffs,
and a self-alignment PAF, then runs `purge_dups -2` and `get_seqs -e`.
It writes a new purged candidate bundle and never changes the raw FASTA.
`assembly_review` receives both the original assembly candidate and the new
purged candidate.

## Failure and Skip Behavior

The stage skips when no materialized raw assembly or long-read input exists.
Missing tools or a failed command are terminal execution failures. All derived
evidence and candidate artifacts are registered in the ledger.

## Verification

Tests cover automatic evidence generation, the fixed `calcuts_auto` and
`get_seqs -e` strategy, raw-assembly preservation, raw-plus-purged candidate
resolution, planner exclusion from `local_exec`, and ledger success events.
