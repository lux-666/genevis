# Pre-HPC Stabilization Design

**Status:** Approved for implementation on 2026-07-31

## Goal

Stabilize the existing ONT-to-Hi-C-to-T2T workflow before its first HPC
validation. This change fixes shared recovery and frozen-command invariants,
aligns the resource and T2T contracts, and deliberately avoids adding new
platforms or stage capabilities.

## Product Path

The first HPC golden path is:

```text
ONT intake
  -> read QC / genome survey
  -> assembly / dedup / review
  -> Hi-C scaffolding and adoption
  -> T2T finishing
```

Hi-C is an upstream workflow dependency of T2T. The T2T stage consumes the
assembly adopted by the Hi-C stage; it does not consume `hic_reads` directly.
The current version does not add a bypass for an externally scaffolded
chromosome assembly.

## Execution Recovery

The durable unit is one frozen attempt, so one shared run-node transition runs
at most one pending attempt. Direct batches retain their ordered cursor list,
but the graph checkpoints after each attempt before routing back to the run
node for the next item. Parameter-owned methods remain one-attempt lifecycles.

Before starting a frozen attempt, the lifecycle may recover canonical execution
evidence already indexed for the same attempt and the same frozen-attempt ref.
Recovered evidence advances the owner cursor without invoking the Runner.
Ambiguous or mismatched evidence fails closed.

This provides no-repeat recovery after canonical evidence is committed. It does
not claim process-level exactly-once behavior if the machine fails after the
external process exits but before any execution evidence can be published.

## Frozen Attempt Boundary

`FrozenAttemptSpec` binds a SHA-256 digest of the retained script. Persistence,
load, and execution validate that digest. The runner executes the exact absolute
`script_path`, while retaining `working_dir` as the process cwd.

The shared evidence boundary requires:

- `working_dir` below `work/<stage>/` for the current run;
- `script_path` below that working directory;
- every `expected_output` below the current run root.

These checks prevent a malformed adapter from executing or cleaning paths
outside the run.

## Resource Contracts

`assembly_dedup` directly requires both the current assembly and long-read
members. The normal ONT pipeline satisfies both, while a run that imports only
an assembly is rejected before purge_dups execution.

The current planner continues to declare long reads for genome survey because
the first supported product path is ONT. General short-or-long alternative
requirements are outside this stabilization change.

T2T documentation distinguishes its workflow dependency on Hi-C scaffolding
from its direct assembly/long-read stage inputs.

## Verification

Local verification covers focused lifecycle, runner, resource-preflight, Hi-C,
and T2T contract tests. The HPC acceptance target is the real Hi-C execution,
human adoption of its chromosome-scale assembly, the exact assembly handoff to
T2T, and the real T2T execution. The upstream ONT assembly path supplies the
input baseline; it is not the primary new HPC validation question.

HPC verification proceeds in two steps:

1. a short environment and input smoke run through the earliest executable
   boundaries;
2. one complete ONT -> Hi-C -> T2T golden-path run with the intended real data.

The first HPC run validates the existing workflow. It is not a benchmark and
does not expand support to HiFi, CLR, CycloneSEQ, or annotation stages.
