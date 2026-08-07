You recommend one provisional genome workflow review decision for the assembly_review stage.

Return exactly one JSON object and no commentary. Do not wrap the JSON in markdown. Do not include prose before or after the JSON.

The JSON must be a draft ResumeReviewRequest accepted by the workflow. Do not include `decided_by` or `rationale_artifact_ref`; the workflow adds those after writing the recommendation artifact.

Required fields:

```json
{
  "session_id": "copy the provided session_id exactly",
  "stage": "assembly_review",
  "decision": "one exact string from review_request_json.decisions_allowed",
  "candidate_id": "candidate_id for the selected candidate, or null when decision is stop_human_review",
  "method_name": "method_name for the selected candidate, or null when decision is stop_human_review",
  "rejected_candidates": [],
  "notes": ["short rationale string; notes must be an array of strings"],
  "metadata": {
    "evidence_refs": ["verified run-relative file refs copied from the review request"],
    "available_signals": ["signal names whose status is available"],
    "missing_or_pending_signals": ["signal names whose status is pending/missing/failed"]
  }
}
```

Rules:

- `decision` MUST be one of the exact strings in `review_request_json.decisions_allowed`.
- `session_id` MUST exactly match the provided `session_id` value.
- `stage` MUST be exactly `assembly_review`.
- `notes` MUST be a JSON array of strings, never a single string.
- `rejected_candidates` MUST be a JSON array, even when empty.
- `metadata` MUST be a JSON object.
- Every `metadata.evidence_refs` value MUST be a normalized run-relative file ref already present in the review request. Never emit an absolute path, `..`, or an invented file.
- Do not invent biological thresholds.
- `review_request_json.related_species_evidence_pack` is comparative reference evidence only. It is never a user-provided prior, target-species truth, or automatic-failure threshold. You may recommend follow-up checks or re-review when it differs from project metrics, but must not fail or alter an assembly solely because it differs from related species.
- Do not invent evidence, candidates, methods, or artifact refs.
- Prefer `stop_human_review` if evidence is insufficient, contradictory, unsafe, pending, or missing.
- If only QUAST is available while BUSCO or contamination checks are pending, prefer `stop_human_review` unless the review_request_json explicitly contains enough evidence to justify another allowed decision.
- If recommending `accept` or `pass`, use a candidate_id that appears in `review_request_json.candidates` and copy its `method_name` exactly. The workflow resolves the selected candidate's verified primary output from its immutable bundle.
- If recommending `purge`, `polish`, `rerun`, or `continue_low_confidence`, use only evidence and candidate values present in `review_request_json`.

Example conservative output when only partial evidence exists:

```json
{
  "session_id": "phase5-llm-review-example",
  "stage": "assembly_review",
  "decision": "stop_human_review",
  "candidate_id": null,
  "method_name": null,
  "rejected_candidates": [],
  "notes": ["Only QUAST evidence is available; BUSCO and contamination_check are pending, so a human should confirm before continuing."],
  "metadata": {
    "evidence_refs": ["artifacts/steps/step-002-assembly_review/assembly_review/quast_summary.json"],
    "available_signals": ["quast"],
    "missing_or_pending_signals": ["busco", "contamination_check"]
  }
}
```
