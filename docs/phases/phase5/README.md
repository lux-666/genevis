# Phase 5 Workflow Hardening And Downstream Backbone

> **For agentic workers:** Phase 5 is not a broad tool-expansion phase. Its
> first priority is keeping the whole workflow continuous, auditable, resumable,
> and safe around human gates.

## Goal

Move from a Phase 4 real local assembly-review mainline to a reliable downstream
workflow backbone.

The guiding principle is:

```text
workflow continuity before method expansion
```

New methods are acceptable only when they preserve the chain:

```text
manifest
-> asset registry
-> stage outputs with refs
-> artifacts
-> ledger
-> human gate when needed
-> resume / continue
```

## Entry Gate

Phase 5 should start only after the Phase 4 Mac-dev / server-pull adaptation is
complete and the server/HPC can run the Phase 4 validation workflow after
`git pull`.

The preferred acceptance host for real QUAST is the server/HPC, not a Mac arm64
machine that lacks `quast.py`.

Phase 5 **task 01 itself ships in code**, but its two server-side checkboxes
(run `scripts/server_check.sh` + `scripts/server_test.sh` after `git pull`,
record the result in `docs/handoff.md`) gate every later Phase 5 task. Do not
start task 02 (durable resume) or task 03 (RepeatMasker backbone) before those
two boxes are checked.

## Initial Task Order

1. [Execution Consistency And Server Gate](01_execution_consistency_and_server_gate.md)
2. [Human Gate And Durable Resume Minimum](02_human_gate_and_durable_resume_minimum.md)
3. [Existing Assembly To RepeatMasker Backbone](03_existing_assembly_repeatmasker_backbone.md)
4. [Review Policy: Strict / Auto Modes And LLM Recommendation](04_review_policy.md)

## Phase 5 Guardrails

- Do not add new bioinformatics tools unless the artifact and ledger path is
  explicit.
- Do not introduce automatic biological thresholds for assembly acceptance,
  purge, polish, Hi-C contact-map acceptance, or annotation merge policy.
- Do not bypass human review gates for generated assemblies or Hi-C scaffold
  candidates.
- Do not restart a workflow from `initial_state(...)` when the user is trying to
  continue a blocked session.
- Do not store large biological outputs, command logs, or metric tables in graph
  state.
- Do not reintroduce local-agent-to-server remote control.

## Deferred Tool Expansion

These are intentionally not the first Phase 5 work items:

- BUSCO / Merqury / minimap2 coverage expansion
- NextDenovo / Canu / hifiasm execution
- polish / purge execution
- raw Hi-C alignment, Juicer, 3D-DNA, Juicebox automation
- RepeatModeler / EDTA / TRF de novo repeat-library execution
- BRAKER3 / MAKER / AUGUSTUS / Miniprot / HISAT2 / StringTie / IsoQuant /
  GeMoMa execution
- InterProScan / eggNOG-mapper / DIAMOND / KofamScan execution
- automatic annotation database downloads

Those should be added only after the first three Phase 5 tasks make the workflow
safe to run, inspect, and resume.

## Verification Baseline

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
