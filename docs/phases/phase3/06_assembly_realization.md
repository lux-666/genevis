# 06 Assembly Realization

Task status: planned.

## Goal

Move `assembly` from candidate bundle planning to the first real ONT/CLR draft
assembly execution.

`assembly` produces candidate assemblies. It should not select the final
assembly; `assembly_review` owns promotion.

## Scope

Start with one default ONT/CLR assembler path, preferably Flye, because it is
the simplest raw long-read draft assembly route.

Candidate methods to evaluate over time:

- `flye_ont`
- `raven_ont_fast`
- `hifiasm_ont`
- `hifiasm_hifi`

For the current ONT/CLR mainline, start with Flye. Raven can be a later fast
ONT candidate. Keep hifiasm out of automatic CycloneSEQ/ONT/CLR planning until
an explicit hifiasm-ONT policy is approved; `hifiasm_hifi` belongs to a HiFi
route, not the ONT/CLR default.

NextDenovo and Canu can remain fallback candidates from the existing catalog, but
they should not be run together with Flye by default.

## Boundaries

- Do not run Flye, NextDenovo, and Canu all by default.
- Do not use hifiasm as the ONT/CLR default.
- Do not polish, purge, scaffold, or annotate in this task.
- Do not choose heavy fallback behavior without a failed primary attempt or
  human decision.
- Do not introduce broad resource scheduling or remote execution.

## Expected Outputs

- `draft_assembly`
- `candidate_assembly`
- assembler log/report artifact refs
- `assembly_bundle`
- candidate provenance that routes into `assembly_review`

## Acceptance

- A small ONT/CLR fixture can produce a real draft assembly through the runner
  boundary or a tested fake assembler path.
- The selected assembler output is registered as `draft_assembly`.
- `assembly_review` receives real assembly refs, not only planned candidate
  JSON.
- Multiple candidate support preserves candidate identity instead of collapsing
  the stage to one untraceable assembly result.
