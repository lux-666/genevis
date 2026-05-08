# 08 Polish And Purge Decision Actions

Task status: planned.

## Goal

Implement the first conditional actions that happen after assembly review:
polish, purge, pass, or stop.

This task absorbs the proposed `assembly_polish` role into post-review actions.
Do not add a new top-level `assembly_polish` stage unless the parent graph
contract is explicitly changed later.

## Scope

Start with one narrow action path. A small polishing route such as NextPolish,
Racon, or Medaka can be selected after deciding which evidence is available and
which tool fits the ONT/CLR route.

Purge actions should remain conditional on review evidence such as duplicated
BUSCO, inflated assembly size, or depth/haplotig signals.

Candidate action methods:

- `minimap2_align_longreads`
- `racon_polish`
- `medaka_consensus`
- `nextpolish_shortread`
- `pilon_shortread`

## Boundaries

- Do not run polish automatically before review.
- Do not run purge_dups without explicit review evidence and human approval.
- Do not overwrite the pre-action assembly.
- Do not silently promote a polished or purged assembly without provenance.
- Do not run short-read polishing tools such as NextPolish/Pilon unless
  appropriate short-read evidence exists.

## Expected Outputs

- optional `polished_assembly`
- optional `purged_assembly`
- `final_assembly_for_scaffolding_or_annotation`
- decision/action ledger records

## Acceptance

- Review decisions can trigger a specific post-review action.
- Old and new assembly refs remain traceable.
- Downstream stages know which assembly was promoted and why.
