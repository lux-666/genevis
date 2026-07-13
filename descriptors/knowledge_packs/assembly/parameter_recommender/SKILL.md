# Assembly Parameter Recommendation

## Pack Metadata

- domain: assembly.parameter_recommendation
- supported_methods: hifiasm, hifiasm_ont
- parameter_model: L0/L1/L2
- primary_inputs: method catalog, read_qc, genome_survey, selected evidence cases, hard validation rules
- output_contract: JSON with params, reasoning, selected_case_ids, omitted_parameters, uncertainty_notes

## Responsibility

Use evidence and conservative assembly experience to recommend only cataloged L2 parameters. Treat L1 values as facts. Never change L0 or L1 parameters, never invent parameter keys, and never use this text as a substitute for hard validation rules.

This pack is not an optimizer. It should choose a small, explainable parameter adjustment set from the complete L2 surface. Omit an L2 parameter when the available evidence does not justify overriding the method catalog default.

## Parameter Levels

- L0: executor, route, output, input, debug, and reproducibility mechanics. These are not shown as adjustable choices.
- L1: readonly facts derived from genome_survey, read_qc, manifest, or run configuration. These can explain a recommendation but cannot be changed.
- L2: behavior-changing assembly parameters exposed by the selected method catalog. Only these may appear in `params`.

## Initial Recommendation Heuristics

For hifiasm, compare the current sample with approved cases using continuous numeric distance for genome size, coverage, read N50, heterozygosity, and ploidy. Treat evidence coverage separately from numeric similarity: missing values never make a case more similar. Prefer exact method/read-type compatibility over broad biological similarity. Methods excerpts may guide parameter choices only when they are present in the selected evidence payload.

Cases are optional evidence, not a prerequisite for recommendation. When no compatible case exists, continue from readonly L1 facts, these curated heuristics, retry context, and catalog defaults. Under weak combined evidence, prefer the default. A parameter override should be tied to at least one of: a strong input fact, a close evidence case, an applicable curated heuristic, or a known failure mode from a previous attempt.

Coverage and read length affect graph connectivity. Low coverage or shorter reads usually argue against aggressive filtering or unusually large k-mer/window choices. High coverage and long reads make stricter overlap or graph-cleaning choices more plausible, but only within catalog and rule boundaries.

Heterozygosity and ploidy affect purging. High heterozygosity or haplotype-rich samples need caution because aggressive purging can erase real haplotypes; duplicated BUSCO or inflated assembly size after review can justify a stronger purge retry.

Genome size affects memory-sensitive knobs. Large genomes or repeat-rich profiles can make overlap and k-mer spectrum settings matter, but memory-related choices should stay conservative unless the failure context points directly to resource exhaustion.

## Retry After Execution Failure

Use the previous attempt, exit code, failure class, and the last log lines to decide whether parameter retry is meaningful. If the log indicates missing inputs, binary errors, permission failures, invalid CLI syntax, or environment problems, do not random-walk parameters; explain the likely non-parameter issue.

For memory exhaustion, reduce graph/overlap pressure or choose memory-saving settings when those parameters exist in the L2 catalog. For parameter parse errors, remove or correct only the parameters implicated by the log. For timeout or stalled overlap stages, prefer small targeted changes rather than many simultaneous overrides.

## Retry After Review Failure

Use review metrics as diagnosis rather than as a new blind search space.

- Under-purged: inflated assembly size, high duplicated BUSCO, or many alternate-like contigs can justify more conservative duplicate handling.
- Over-purged: low assembly size ratio, missing BUSCO, or collapsed haplotypes argue against aggressive purging or filtering.
- Fragmented: high contig count and low N50 can justify preserving connectivity, reducing over-cleaning, or revisiting overlap sensitivity.
- Low completeness: distinguish missing biological content from contamination or over-filtering before changing many knobs.

## Output Discipline

Return JSON only. Put only L2 keys under `params`. Keep reasoning short and tied to current facts, selected case IDs, rules, curated heuristics, or retry context. Include every selected case you used in `selected_case_ids`; leave it empty if none were informative. When returning `parameter_evidence`, use only `case`, `profile_fact`, `skill_heuristic`, `retry_failure`, or `catalog_default`; case/span citations are required only for `case` evidence.
