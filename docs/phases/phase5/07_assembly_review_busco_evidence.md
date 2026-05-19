# 07 Assembly Review Evidence Expansion, BUSCO First

Task status: planned.

## Goal

Make `assembly_review` less blind by adding one real orthology-completeness
evidence slice before broader review automation.

Current real review evidence is centered on QUAST. BUSCO should become the next
server-validated signal so human reviewers and LLM provisional review are not
asked to judge assemblies from contiguity metrics alone.

## Scope

- Add only one new real execution slice: `assembly_review.eval.busco`.
- Require the user/operator to provide an offline BUSCO lineage path and
  provenance. Do not download lineages automatically.
- Run BUSCO through the existing execution pattern:

```text
descriptor
-> frozen attempt
-> env_dispatch / EnvResolver preflight
-> LocalRunner
-> artifacts
-> ledger
-> compact state refs
```

- Register BUSCO outputs and a compact parsed summary artifact.
- Add BUSCO as a signal in `assembly_review_request.json`.
- Update auto review so `auto_decidable` requires enough available evidence for
  the configured policy. Initially, QUAST + BUSCO should satisfy this better
  than QUAST alone; missing BUSCO should make auto review more conservative.

## Non-Goals

- Do not add Merqury, minimap2 coverage, purge_dups, or contamination checks in
  this task.
- Do not add automatic accept/reject thresholds.
- Do not download BUSCO lineages or databases.
- Do not let BUSCO failure reject a candidate automatically.
- Do not add assembler rerun/optimization loops here; that is task 08.

## Expected Outputs

- `config/envs.yaml` method entry for `assembly_review.eval.busco` with
  required `busco` binary.
- Run-config or manifest surface for BUSCO lineage path and provenance.
- Frozen BUSCO attempt scripts that run locally through `env_dispatch`.
- Registered BUSCO artifacts, including raw BUSCO output refs and compact
  summary JSON.
- Assembly review request signal with `method_name=assembly_review.eval.busco`,
  `status`, metric refs, and provenance.
- Tests for missing lineage, missing BUSCO tool, successful fake BUSCO output,
  failed BUSCO output, and auto-review conservatism when BUSCO is absent.

## Tasks

- [ ] Define the BUSCO run-config/provenance contract for offline lineage paths.
- [ ] Add method-level env resolution and preflight for BUSCO.
- [ ] Freeze and run the BUSCO attempt through `LocalRunner`.
- [ ] Parse the BUSCO short summary into compact JSON without applying
  thresholds.
- [ ] Register raw and summary BUSCO artifacts.
- [ ] Add BUSCO signals to `assembly_review_request.json`.
- [ ] Update `_assembly_review_auto_decidable` and tests so QUAST-only evidence
  is conservative and QUAST+BUSCO can be auto-decidable when no disqualifying
  signals are present.
- [ ] Add a server/HPC smoke with offline lineage path supplied by the operator.

## Acceptance

- A real server run can produce QUAST and BUSCO evidence for the same assembly
  candidate.
- BUSCO evidence is visible in review requests, inspect output, ledger, and
  artifacts.
- Human and LLM review still make decisions through the existing review path.
- No biological threshold is encoded as automatic accept/reject logic.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly_review tests.test_run_config tests.test_entrypoints tests.test_env_resolver -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
