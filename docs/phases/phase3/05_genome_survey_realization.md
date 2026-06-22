# 05 Genome Survey Realization

Task status: implemented.

## Goal

Make `genome_survey` a real conditional stage for projects with available short
or long DNA reads, including ONT/CLR-only workflows.

## Scope

Real execution should start with one narrow k-mer survey path, for example
Jellyfish or Meryl plus GenomeScope2.

Candidate methods:

- `jellyfish_count`
- `meryl_count`
- `genomescope2_model`
- `smudgeplot_model`

Allowed modes:

- full survey when short or long DNA reads exist;
- explicit skip/failure only when no DNA reads are available.

Deferred mode:

- skip when the user provides a trusted `known_genome_size`; this is deferred until
  the manifest/state contract has an explicit trusted-size marker.

## Boundaries

- Run the selected Jellyfish + GenomeScope2 survey path for ONT/CLR long reads
  when short reads are unavailable.
- Do not allow assembly to start without either an explicit user genome size or
  a survey-derived genome size.
- Do not model long-read-only survey as a fallback; it is an intentional survey
  input mode.
- Do not invent genome-size confidence thresholds.
- Trigger a `genome_size_conflict` review only after a real survey exists and
  conflicts with a trusted manifest/user genome-size estimate.

## Expected Outputs

- `genome_survey_result`
- `kmer_hist`
- `genome_size_estimate`
- `heterozygosity_estimate`
- `repeat_estimate`
- `genome_survey_bundle`
- k-mer histogram/report refs when full survey runs
- explicit skip/failure notes when survey cannot run

## Tasks

- [x] Preserve full survey execution for available short or long DNA reads.
- [x] Add one opt-in local execution route for full survey: Jellyfish k-mer count/histogram followed by GenomeScope2 modeling.
- [x] Require explicit local `survey_parameters` for `kmer_size` and `hash_size`; no hidden k-mer/resource defaults or confidence thresholds are added.
- [x] Keep Meryl and Smudgeplot as registered candidates only; they are not executed in this task.
- [x] Keep ONT/CLR-only workflows on the same Jellyfish + GenomeScope2 survey path.
- [x] Record local execution attempts, artifacts, and command logs through the existing runner/artifact/ledger boundary.
- [x] Compact committed `genome_survey` state before advancing to assembly.

## Implemented State

- `app/stages/genome_survey/nodes.py` now keeps default full mode as a planned contract unless `stage_outputs.genome_survey.execution_mode == "local"` is explicitly set.
- Local full mode validates existing Jellyfish and GenomeScope2 descriptors, freezes deterministic attempts, runs them through `LocalRunner`, registers k-mer DB, k-mer histogram, GenomeScope2 summary, stdout/stderr, and final result artifacts, then records ledger events.
- Local execution requires explicit `survey_parameters.kmer_size` and `survey_parameters.hash_size`; missing or invalid values fail before external tools are invoked.
- The only executed full-survey route is Jellyfish + GenomeScope2. Meryl, Smudgeplot, trusted `known_genome_size` skip semantics, genome-size conflict review, and broad execution remain deferred. In-agent remote execution is intentionally unsupported (see `docs/deployment/mac_dev_server_pull.md`).
- `commit_genome_survey` now writes a compact stage output, dropping resolved asset payloads, local env, survey parameters, frozen attempts, execution results, and transient tool paths.
- `tests/test_genome_survey.py` covers default planning with explicit user size, fake-binary local execution, missing local parameters, ONT/CLR local execution, explicit prior precedence, genome-size-unavailable failure, artifact registration, ledger attempts, and compact committed state.

改动要点：

- `app/stages/genome_survey/builder.py` and `routing.py` split full-mode planning from the local Jellyfish/GenomeScope2 execution branch while leaving fallback and skip paths unchanged.
- `app/schemas/artifacts.py` allows completed genome survey results and defines artifact type constants for k-mer DB, k-mer histogram, and GenomeScope2 summary outputs.
- Local execution parses only direct numeric key/value GenomeScope2 estimates when available; if genome size remains unavailable and no explicit user prior exists, the stage fails with `GENOME_SIZE_UNAVAILABLE`.

## Acceptance

- Full survey runs from short or long DNA reads.
- ONT/CLR-only workflows proceed to assembly only with an explicit user genome size or a survey-derived genome size.
- Assembly can consume survey output as a prior, not as an input file.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_genome_survey -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_genome_survey tests.test_assembly tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
