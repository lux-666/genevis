# 07 Assembly Review Evidence Expansion, BUSCO First

Task status: implemented. Server/HPC smoke pending.

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

- [x] Define the BUSCO run-config/provenance contract for offline lineage paths.
- [x] Add method-level env resolution and preflight for BUSCO.
- [x] Freeze and run the BUSCO attempt through `LocalRunner`.
- [x] Parse the BUSCO short summary into compact JSON without applying
  thresholds.
- [x] Register raw and summary BUSCO artifacts.
- [x] Add BUSCO signals to `assembly_review_request.json`.
- [x] Update `_assembly_review_auto_decidable` and tests so QUAST-only evidence
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

## Implementation Notes

- BUSCO parameters are supplied through `--run-config` under
  `assembly_review.busco`; the contract requires `lineage_path`,
  `lineage_name`, `lineage_version`, `lineage_source`, `mode="genome"`, and
  positive `threads`.
- `config/envs.yaml` now has a method-level `assembly_review.eval.busco` entry
  requiring the `busco` binary. Local execution uses the existing
  `EnvResolver` preflight and `LocalRunner` path.
- Missing lineage parameters or a missing lineage path marks BUSCO as a
  `missing` review signal and keeps review conservative; it does not reject the
  candidate.
- Successful BUSCO runs register the raw short summary and a compact
  `busco_summary.json` with parsed percentages/counts plus lineage provenance.
- `assembly_review_request.json` carries BUSCO `available` / `missing` /
  `failed` signals alongside QUAST. BUSCO failure is evidence for review only,
  not an automatic reject.
- `_assembly_review_auto_decidable` now requires QUAST and BUSCO evidence for
  every candidate and still refuses auto-decision when contamination evidence is
  available.
- Example run-config:
  `docs/examples/phase5_busco_run_config.template.json`.
- Server smoke script: `scripts/server_phase5_busco_smoke.sh`.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly_review tests.test_run_config tests.test_entrypoints tests.test_env_resolver -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
