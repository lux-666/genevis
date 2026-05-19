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

Phase 5 task 01 is accepted with server/HPC validation as of 2026-05-15.
The server record is session `phase4-real-bht-20260515-e` in
`docs/handoff.md`: Flye executed, QUAST produced real metrics and artifacts,
and durable resume advanced until the expected function-annotation database
provenance gate.

## Initial Task Order

1. [Execution Consistency And Server Gate](01_execution_consistency_and_server_gate.md)
2. [Human Gate And Durable Resume Minimum](02_human_gate_and_durable_resume_minimum.md)
3. [Existing Assembly To RepeatMasker Backbone](03_existing_assembly_repeatmasker_backbone.md)
4. [Review Policy: Strict / Auto Modes And LLM Recommendation](04_review_policy.md)
5. [Operator Usability Pack](05_operator_usability_pack.md)
6. [Provided Gene Annotation Adoption](06_provided_gene_annotation_adoption.md)
7. [Assembly Review Evidence Expansion, BUSCO First](07_assembly_review_busco_evidence.md)
8. [Assembly Parameter Loop And Candidate Comparison](08_assembly_parameter_loop.md)
9. [Assembly Review Visual Report And Human Selection](09_assembly_review_visual_report.md)
10. [Function Annotation Provenance Completion](10_function_annotation_provenance_completion.md)

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

- Merqury / minimap2 coverage expansion
- NextDenovo / Canu / hifiasm execution
- polish / purge execution
- raw Hi-C alignment, Juicer, 3D-DNA, Juicebox automation
- RepeatModeler / EDTA / TRF de novo repeat-library execution
- BRAKER3 / MAKER / AUGUSTUS / Miniprot / HISAT2 / StringTie / IsoQuant /
  GeMoMa execution
- InterProScan / eggNOG-mapper / DIAMOND / KofamScan execution
- automatic annotation database downloads

Those should be added only after Phase 5 has made the workflow safe to run,
inspect, resume, and complete through honest user-provided annotation and
database provenance paths.

## Verification Baseline

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
