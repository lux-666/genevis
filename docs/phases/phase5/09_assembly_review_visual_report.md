# 09 Assembly Review Visual Report And Human Selection

Task status: implemented.

## Goal

Make assembly review decisions easier by producing a human-readable visual
report over assembly candidates and their evidence, then routing the selected
candidate through the existing human gate.

This task turns the parameter loop evidence into a product-quality review
surface without introducing a web UI or automatic biological thresholds.

## Scope

- Generate static report artifacts for assembly review and candidate comparison.
- Include plots/tables for compact metrics that already exist as artifacts:
  - contig count / total length / N50 / largest contig from QUAST
  - BUSCO completeness categories when task 07 is implemented
  - per-candidate Flye parameter provenance
  - route/evidence completeness and missing signals
- Include links/refs to raw reports, not embedded large outputs.
- Add human gate decision templates that reference candidate IDs and report
  artifacts.
- Keep the report format portable for server/HPC use, such as HTML plus JSON or
  Markdown plus image assets.

## Non-Goals

- Do not build a long-running web UI.
- Do not encode pass/fail thresholds in plots.
- Do not hide missing evidence; missing BUSCO/QUAST should be visible.
- Do not require browser automation for core workflow correctness.

## Expected Outputs

- `assembly_review_report` artifact for single-candidate review.
- `assembly_candidate_comparison_report` artifact for rerun loops.
- Static plots or tables generated from compact summary JSON.
- Inspect output points operators to the latest review report.
- Decision templates include candidate IDs, report refs, and allowed decisions.

## Tasks

- [x] Define the report artifact schema and stable output paths.
- [x] Add report generation from QUAST/BUSCO compact summaries and candidate
  parameter provenance.
- [x] Add report refs to review request payloads and inspect next-action output.
- [x] Extend decision templates with report refs and candidate comparison
  context.
- [x] Add tests for report generation with QUAST-only, QUAST+BUSCO, missing
  signal, and multi-candidate inputs.
- [x] Add a server/HPC smoke that produces a report artifact and demonstrates
  human selection from the report.

## Implementation Key Points

- Assembly review now writes stable static report artifacts before the human gate:
  `assembly_review_report.md` for the operator-facing table and
  `assembly_review_report.json` for compact machine-readable report data.
- Multi-candidate rerun loops additionally write
  `assembly_candidate_comparison_report.md` and keep the existing
  `candidate_comparison.json` available as raw comparison context.
- Reports are generated only from compact candidate, QUAST/BUSCO summary, signal
  status, and parameter-provenance data. Raw command output and large reports are
  referenced by artifact path rather than embedded.
- Missing, failed, and pending evidence are shown explicitly; no biological
  pass/fail thresholds or automatic candidate selection are encoded in the
  report.
- Review request payloads, active interrupts, durable inspect requirements,
  `next_action.relevant_refs`, decision-template guidance, and blocked operator
  reports now carry `report_refs` plus candidate comparison context.
- Report paths are stable and replay-safe: if content is unchanged and the
  artifact index already has the matching record, replay reuses the existing
  report without rewriting it or appending duplicate report artifact records.

## Acceptance

- A reviewer can choose a final assembly candidate from an artifact report
  without reading raw command output.
- The report shows available and missing evidence honestly.
- Human gate semantics remain unchanged: the report informs the decision but
  does not make it.
- The report is registered in artifacts and referenced by inspect output.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly_review tests.test_review_report tests.test_entrypoints -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
