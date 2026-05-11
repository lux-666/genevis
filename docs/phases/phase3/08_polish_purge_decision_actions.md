# 08 Polish And Purge Decision Actions

Task status: partially implemented (post-review action contract slice).

## Goal

Implement the first conditional actions that happen after assembly review:
polish, purge, pass, or stop.

This task absorbs the proposed `assembly_polish` role into post-review actions.
Do not add a new top-level `assembly_polish` stage unless the parent graph
contract is explicitly changed later.

## Scope

Start with one narrow action path. This slice implements the safe contract and
provenance layer for post-review actions inside `assembly_review`:

- `pass` and legacy `accept` promote the selected assembly unchanged.
- `continue_low_confidence` promotes the selected assembly unchanged with a
  low-confidence action type.
- `purge` and `polish` can promote a human-supplied transformed assembly only
  when the resume request includes a materialized output ref, explicit human
  approval, evidence metadata, method/provenance metadata, and the pre-action
  assembly remains traceable.
- `stop_human_review` remains terminal.

A small polishing route such as NextPolish, Racon, or Medaka can be selected
after deciding which evidence is available and which tool fits the ONT/CLR
route. Local execution for those tools is not implemented in this slice.

Purge actions remain conditional on review evidence such as duplicated BUSCO,
inflated assembly size, or depth/haplotig signals. This slice does not execute
`purge_dups` as an assembly-transforming action; it only permits externally
materialized purge outputs to be promoted with explicit human approval,
evidence metadata, and method/provenance metadata.

Candidate action methods:

- `minimap2_align_longreads`
- `racon_polish`
- `medaka_consensus`
- `nextpolish_shortread`
- `pilon_shortread`

## Boundaries

- Do not run polish automatically before review.
- Do not run `purge_dups` without explicit review evidence and human approval.
- Do not overwrite the pre-action assembly.
- Do not silently promote a polished or purged assembly without provenance.
- Do not run short-read polishing tools such as NextPolish/Pilon unless
  appropriate short-read evidence exists.

## Expected Outputs

- optional `polished_assembly`
- optional `purged_assembly`
- `final_assembly_for_scaffolding_or_annotation`
- decision/action ledger records

## Implemented State

- `app/review/base.py` allows `pass` and `polish` review decisions while keeping
  `accept` as a pass-through compatibility alias.
- `app/schemas/artifacts.py` defines `assembly_review.action` and
  `AssemblyReviewActionArtifact` to record pre-action assembly, post-action
  assembly, action type, execution mode, evidence refs/notes, and downstream
  final assembly ref.
- `app/stages/assembly_review/nodes.py` publishes
  `final_assembly_for_scaffolding_or_annotation` for accepted/passed assemblies
  and writes an `assembly_review_action.json` artifact for every non-stop
  committed decision.
- `purge` no longer behaves like `accept`: it requires a materialized transformed
  output ref, explicit human approval metadata, evidence metadata, and
  method/provenance metadata before promotion. If `final_assembly_ref` and
  `metadata.action_output_ref` are both supplied, they must match.
- `polish` follows the same provenance gate; obvious short-read polishing method
  names such as `nextpolish_shortread` and `pilon_shortread` require explicit
  short-read evidence metadata.
- Local polishing and purge execution remain deferred. No Racon, Medaka,
  NextPolish, Pilon, or purge_dups action runner is added here.

## Acceptance

- Review decisions can trigger a specific post-review action.
- Old and new assembly refs remain traceable.
- Downstream stages know which assembly was promoted and why.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_review_resume tests.test_assembly_review -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_hic_scaffolding tests.test_repeat_annotation -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
