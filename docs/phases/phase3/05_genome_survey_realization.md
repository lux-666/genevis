# 05 Genome Survey Realization

Task status: implemented.

## Goal

Make `genome_survey` a real conditional stage for projects with low-error short
DNA reads while keeping ONT/CLR-only workflows non-blocking.

## Scope

Real execution should start with one narrow short-read k-mer survey path, for
example Jellyfish or Meryl plus GenomeScope2.

Candidate methods:

- `jellyfish_count`
- `meryl_count`
- `genomescope2_model`
- `smudgeplot_model`

Allowed modes:

- full survey when `clean_short_dna_reads` exists;
- explicit skip or low-confidence prior when only ONT/CLR long reads exist.

Deferred mode:

- skip when the user provides a trusted `known_genome_size`; this is deferred until
  the manifest/state contract has an explicit trusted-size marker.

## Boundaries

- Do not run Jellyfish, Meryl, GenomeScope2, or Smudgeplot on ONT/CLR long reads
  by default.
- Do not make genome survey a hard prerequisite for assembly.
- Do not treat a long-read-only fallback as a real k-mer survey.
- Do not invent genome-size confidence thresholds.
- Trigger a `genome_size_conflict` review only after a real short-read survey
  exists and conflicts with a trusted manifest/user genome-size estimate.

## Expected Outputs

- `genome_survey_result`
- `kmer_hist`
- `genome_size_estimate`
- `heterozygosity_estimate`
- `repeat_estimate`
- `genome_survey_bundle`
- k-mer histogram/report refs when full survey runs
- explicit skip or low-confidence notes when full survey cannot run

## Tasks

- [x] Preserve the existing full/fallback/skip mode selection so only `clean_short_dna_reads` can enter full survey.
- [x] Add one opt-in local execution route for full survey: Jellyfish k-mer count/histogram followed by GenomeScope2 modeling.
- [x] Require explicit local `survey_parameters` for `kmer_size`, `read_length`, and `hash_size`; no hidden k-mer/resource defaults or confidence thresholds are added.
- [x] Keep Meryl and Smudgeplot as registered candidates only; they are not executed in this task.
- [x] Keep ONT/CLR-only workflows non-blocking with low-confidence fallback records and no k-mer tool execution.
- [x] Record local execution attempts, artifacts, and command logs through the existing runner/artifact/ledger boundary.
- [x] Compact committed `genome_survey` state before advancing to assembly.

## Implemented State

- `app/stages/genome_survey/nodes.py` now keeps default full mode as a planned contract unless `stage_outputs.genome_survey.execution_mode == "local"` is explicitly set.
- Local full mode validates existing Jellyfish and GenomeScope2 descriptors, freezes deterministic attempts, runs them through `LocalRunner`, registers k-mer DB, k-mer histogram, GenomeScope2 summary, stdout/stderr, and final result artifacts, then records ledger events.
- Local execution requires explicit `survey_parameters.kmer_size`, `survey_parameters.read_length`, and `survey_parameters.hash_size`; missing or invalid values fail before external tools are invoked.
- The only executed full-survey route is Jellyfish + GenomeScope2. Meryl, Smudgeplot, trusted `known_genome_size` skip semantics, genome-size conflict review, RemoteRunner, and broad execution remain deferred.
- `commit_genome_survey` now writes a compact stage output, dropping resolved asset payloads, local env, survey parameters, frozen attempts, execution results, and transient tool paths.
- `tests/test_genome_survey.py` covers default planning, fake-binary local execution, missing local parameters, ONT/CLR local-mode guard behavior, fallback, skip, artifact registration, ledger attempts, and compact committed state.

改动要点：

- `app/stages/genome_survey/builder.py` and `routing.py` split full-mode planning from the local Jellyfish/GenomeScope2 execution branch while leaving fallback and skip paths unchanged.
- `app/schemas/artifacts.py` allows completed genome survey results and defines artifact type constants for k-mer DB, k-mer histogram, and GenomeScope2 summary outputs.
- Local execution parses only direct numeric key/value GenomeScope2 estimates when available; unparseable fields remain `None` with notes, and no manifest/user prior comparison or review threshold is invented.

## Acceptance

- Full survey runs only from short DNA reads.
- ONT/CLR-only workflows proceed to assembly with an explicit survey status.
- Assembly can consume survey output as a prior, not as an input file.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_genome_survey -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_genome_survey tests.test_assembly tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
