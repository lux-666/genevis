# Assembly Parameter Recommendation

## Pack Metadata

- domain: assembly.parameter_recommendation
- supported_methods: flye, nextdenovo, hifiasm, hifiasm_ont
- parameter_model: L0/L1/L2
- primary_inputs: method catalog, read_qc, genome_survey, selected evidence cases, hard validation rules
- output_contract: selected method prompt bundle in app/llm

## Responsibility

Use evidence and conservative assembly experience to recommend cataloged L2 parameters. Treat L0 and L1 values as immutable context. Hard validation remains the authority for parameter levels, keys, values, and evidence references.

This pack is not an optimizer. It should choose a small, explainable parameter adjustment set from the complete L2 surface. Omit an L2 parameter when the available evidence does not justify overriding the method catalog default.

## Parameter Levels

- L0: executor, route, output, input, debug, and reproducibility mechanics. These are not shown as adjustable choices.
- L1: readonly facts derived from genome_survey, read_qc, manifest, or run configuration. These can explain a recommendation but cannot be changed.
- L2: behavior-changing assembly parameters exposed by the selected method catalog. Only these may appear in `params`.

## Initial Recommendation Heuristics

For hifiasm, compare the current sample with approved cases using continuous numeric distance for genome size, coverage, read N50, heterozygosity, and ploidy. Treat evidence coverage separately from numeric similarity: similarity is based on the values present in both profiles. Prefer exact method/read-type compatibility over broad biological similarity. Methods excerpts may guide parameter choices when they are present in the selected evidence payload.

Cases are optional evidence, not a prerequisite for recommendation. When no compatible case exists, continue from readonly L1 facts, these curated heuristics, retry context, and catalog defaults. Under weak combined evidence, prefer the default. A parameter override should be tied to at least one of: a strong input fact, a close evidence case, an applicable curated heuristic, or a known failure mode from a previous attempt.

Coverage and read length affect graph connectivity. Low coverage or shorter reads usually argue against aggressive filtering or unusually large k-mer/window choices. High coverage and long reads make stricter overlap or graph-cleaning choices more plausible, but only within catalog and rule boundaries.

Heterozygosity and ploidy affect purging. High heterozygosity or haplotype-rich samples need caution because aggressive purging can erase real haplotypes; duplicated BUSCO or inflated assembly size after review can justify a stronger purge retry.

Genome size affects memory-sensitive knobs. Large genomes or repeat-rich profiles can make overlap and k-mer spectrum settings matter, but memory-related choices should stay conservative unless the failure context points directly to resource exhaustion.

## Retry After Execution Failure

Use the previous attempt, exit code, failure class, and the last log lines to decide whether parameter retry is meaningful. Treat missing inputs, binary errors, permission failures, invalid CLI syntax, and environment problems as non-parameter issues and keep the parameter set stable.

For memory exhaustion, reduce graph/overlap pressure or choose memory-saving settings when those parameters exist in the L2 catalog. For parameter parse errors, remove or correct only the parameters implicated by the log. For timeout or stalled overlap stages, prefer small targeted changes rather than many simultaneous overrides.

## Retry After Review Failure

Use review metrics as diagnosis rather than as a new blind search space.

- Under-purged: inflated assembly size, high duplicated BUSCO, or many alternate-like contigs can justify more conservative duplicate handling.
- Over-purged: low assembly size ratio, missing BUSCO, or collapsed haplotypes argue against aggressive purging or filtering.
- Fragmented: high contig count and low N50 can justify preserving connectivity, reducing over-cleaning, or revisiting overlap sensitivity.
- Low completeness: distinguish missing biological content from contamination or over-filtering before changing many knobs.

## Response Guidance

Follow the selected method example supplied by `app/llm`. Keep reasoning short and tied to current facts, selected case IDs, rules, curated heuristics, or retry context. Record the selected cases and evidence references that actually support the recommendation.
