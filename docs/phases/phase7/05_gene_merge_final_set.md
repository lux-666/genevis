# 07-05 Candidate Merge And Final Gene Set

Task status: planning (subject captured; specifics loose).

## Goal

Merge candidate gene models from one or more executed gene methods into a final
gene set, gated by the candidate-evidence and merge reviews, and validated by
BUSCO. This is where EVM weights are actually decided — not earlier.

## Position In The Chain

```
run gene method(s) → candidate-evidence review → merge (EVM/MAKER) → final gene-set review + BUSCO
```

EVM weighting is a **merge-time** decision. The candidate-evidence review (task
03) confirms candidates are complete and worth merging; the merge step then
combines them; the final-gene-set review accepts/rejects the merged set with
BUSCO as a signal.

## Current State

- No merge executor exists. `gene_annotation` has no EVM/MAKER integration today.
- BUSCO already appears as an assembly-review evidence signal (Phase 5 task 07);
  reuse that integration pattern for gene-set completeness rather than inventing
  a new one.

## Scope

- A merge executor (EVM and/or MAKER-style) that takes candidate gene models +
  evidence weights and produces a merged final gene set as durable artifacts.
- Wire EVM weights as the decision surface at the merge review gate.
- Run BUSCO on the final gene set and feed it into the final-gene-set review.

## Non-Goals

- Producing the candidates (task 04).
- Function annotation (task 06).

## Open Questions

- EVM vs MAKER as the merge backbone; stage contract flags EVM maintenance status
  as a risk and treats it as optional compatibility.
- Whether merge is its own stage/node or part of `gene_annotation`'s commit path
  (ties to task 03's open question).
- BUSCO lineage selection for gene-set mode and how it is configured.
