# Assembly Parameter Recommendation

Phase 10 can recommend assembly parameters for hifiasm and hifiasm_ont before a local attempt is frozen and run. It uses read_qc facts, genome_survey facts, selected evidence cases, hard validation rules, and the selected method catalog.

## What It Can Change

Parameters use three levels:

- L0: the LLM does not see or modify executor and route mechanics, such as input reads, output prefix, threads, attempt directories, and fixed route flags.
- L1: the LLM sees but does not modify facts, such as genome size, coverage, read N50, read quality, heterozygosity, and hom coverage.
- L2: the LLM sees and may recommend behavior-changing assembly parameters, such as purge, overlap, k-mer, filtering, and graph-cleaning knobs.

Only L2 keys can appear in a recommendation. If the recommendation includes an unknown key or tries to change L0/L1, validation fails and the command is not run.

## Why Parameters May Be Unset

The recommender is not trying to fill every possible CLI flag. If evidence is weak, it may omit an L2 parameter. An omitted L2 parameter means "do not explicitly set this flag"; the assembler's default behavior applies.

The MVP exposes every cataloged L2 parameter to the prompt instead of maintaining a hidden subset. This makes the method catalog the single place to decide what is in scope.

## Retries

If command execution fails, the next recommendation can use the previous attempt, exit code, failure class, and last log lines. It should only change parameters when the failure looks parameter-related; missing files, binary failures, permission errors, and environment problems are not solved by random parameter changes.

If assembly_review metrics fail the quality gate, the next recommendation can use the previous attempt and review diagnosis. Examples include under-purging, over-purging, fragmentation, or low completeness. Retry caps prevent infinite loops.

## Where To Find Records

Attempt artifacts live under:

```text
data/artifacts/assembly/<session>/parameter_attempt_<N>/
```

Typical files are:

```text
input_profile.json
prompt_payload.json
llm_response.json
validation.json
command.json
review_failure.json
```

The run state also contains `parameter_attempts`, a compact replay ledger with the attempt id, method id, status, selected cases, recommended and omitted parameters, validation result, command argv, and optional execution or review failure context.

These artifacts are session/attempt-scoped because retries must be immutable and easy to compare. Hashes are recorded for integrity checks; the path refs are the runtime replay locations.

## Removing A Parameter From Consideration

To keep an L2 parameter out of recommendation, edit the selected method catalog and remove that parameter from the catalog:

```text
descriptors/knowledge_packs/assembly/methods/hifiasm/params.yaml
descriptors/knowledge_packs/assembly/methods/hifiasm_ont/params.yaml
```

Do not add a second hidden prompt filter. If a behavior-changing parameter remains in the catalog as L2, it is part of the recommender surface.

## Current Limits

The hifiasm/hifiasm_ont loop, runtime help verification hook, descriptor-derived planner metadata, architecture guards, and standalone command/review-failure artifact files are integrated. Remaining limits are practical: binary-backed catalog verification only runs when `HIFIASM_BIN` or `hifiasm` is available, and broader closed-loop coverage can still be expanded as more real samples are exercised.
