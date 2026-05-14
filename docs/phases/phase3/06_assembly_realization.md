# 06 Assembly Realization

Task status: implemented.

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

## Tasks

- [x] Preserve existing assembly candidate planning and hifiasm explicit-only compatibility behavior.
- [x] Add one explicit local execution route for the primary Flye candidate.
- [x] Keep NextDenovo and Canu as planned fallback candidates only; do not execute them with Flye by default.
- [x] Register real Flye draft assembly FASTA, command logs, optional Flye log, and updated candidate bundle artifacts.
- [x] Record local Flye attempt lifecycle through the existing ledger boundary.
- [x] Keep assembly output compact before handing candidate refs to `assembly_review`.
- [x] Preserve `assembly_review` as the promotion gate; executed assembly candidates still block for human review.

## Implemented State

- `app/stages/assembly/nodes.py` keeps default assembly behavior as candidate planning unless `stage_outputs.assembly.execution_mode == "local"` is explicitly set.
- The only executable assembly route is the selected primary `assembly.candidate.flye` candidate. The frozen script runs `flye --nano-raw <reads...> --out-dir <out_dir> --threads <threads>`, copies Flye's `assembly.fasta` to the registered draft assembly artifact, and records command stdout/stderr.
- Local Flye execution accepts either a registered `clean_long_reads` path or a read QC FOFN handoff. `flye_parameters.threads` may be supplied; invalid non-positive or non-integer values fail before Flye is invoked, and omitted threads use one thread for the narrow local route.
- Executed Flye sets assembly stage status to `draft_ready`; this means a real draft candidate has been produced and routed to review, not that the assembly has been promoted as final.
- Executed Flye candidate bundles keep their candidate identity and point `expected_outputs.draft_assembly_fasta` at the real FASTA artifact consumed by `assembly_review`.
- Flye failures record `attempt_failed`, remove the expected draft FASTA if present, and do not automatically run NextDenovo or Canu.
- NextDenovo, Canu, Raven, hifiasm-ONT default behavior, broad resource scheduling, polishing, purging, scaffolding, and annotation execution remain deferred. In-agent remote execution is intentionally unsupported (see `docs/deployment/mac_dev_server_pull.md`).
- `tests/test_assembly.py` covers default non-execution, fake-Flye local success, read-QC FOFN input, failure without fallback execution, hifiasm non-default behavior, artifact/ledger records, and compact committed state.
- `tests/test_assembly_review.py` verifies that an executed assembly-stage candidate passes a real FASTA ref into the existing human review gate.

改动要点：

- `app/stages/assembly/builder.py` and `routing.py` split default candidate planning from the explicit local Flye execution branch.
- `app/schemas/artifacts.py` defines assembly draft/candidate artifact constants and allows executed candidate bundles.
- Planned candidate bundles mark `draft_assembly_ref_status: planned_path_not_materialized`; executed Flye bundles update that marker to `materialized`.
- `descriptors/methods/assembly/flye_candidate.yaml` now describes planned-or-explicit-local Flye behavior and explicitly records the supported clean-read-or-FOFN input contract.

## Acceptance

- A small ONT/CLR fixture can produce a real draft assembly through the runner
  boundary or a tested fake assembler path.
- The selected assembler output is registered as `draft_assembly`.
- `assembly_review` receives real assembly refs, not only planned candidate
  JSON.
- Multiple candidate support preserves candidate identity instead of collapsing
  the stage to one untraceable assembly result.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_genome_survey tests.test_assembly tests.test_assembly_review tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
