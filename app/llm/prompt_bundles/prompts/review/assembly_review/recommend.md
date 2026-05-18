Read the review request JSON and recommend one provisional assembly review decision.

Return only one JSON object. Do not wrap it in markdown. Do not include prose outside the JSON.

Required output fields:

```json
{
  "session_id": "copy the session_id value provided above the review_request_json block",
  "stage": "assembly_review",
  "decision": "one exact string from review_request_json.decisions_allowed",
  "candidate_id": "candidate_id for the selected candidate, or null when decision is stop_human_review",
  "method_name": "method_name for the selected candidate, or null when decision is stop_human_review",
  "final_assembly_ref": "final_assembly_ref for the selected candidate, or null when decision is stop_human_review",
  "rejected_candidates": [],
  "notes": ["short rationale string; notes must be an array of strings"],
  "metadata": {
    "evidence_refs": ["artifact refs used for the recommendation"],
    "available_signals": ["signal names whose status is available"],
    "missing_or_pending_signals": ["signal names whose status is pending/missing/failed"]
  }
}
```

Rules:

- `decision` MUST be one of the exact strings in `review_request_json.decisions_allowed`.
- `session_id` MUST exactly match the `session_id:` value provided in the user message.
- `stage` MUST be exactly `assembly_review`.
- `notes` MUST be a JSON array of strings, never a single string.
- `rejected_candidates` MUST be a JSON array, even when empty.
- `metadata` MUST be a JSON object.
- Do not include `decided_by` or `rationale_artifact_ref`; the workflow will add those after writing this recommendation artifact.
- Do not invent biological thresholds.
- Prefer `stop_human_review` if evidence is insufficient, contradictory, or unsafe to decide.
- If only QUAST is available while BUSCO/contamination checks are pending, prefer `stop_human_review` unless the review_request_json explicitly contains enough evidence to justify another allowed decision.
- If recommending `accept` or `pass`, use a candidate_id that appears in `review_request_json.candidates` and copy its `method_name` and `final_assembly_ref` exactly.

Example conservative output when only partial evidence exists:

```json
{
  "session_id": "phase5-llm-review-example",
  "stage": "assembly_review",
  "decision": "stop_human_review",
  "candidate_id": null,
  "method_name": null,
  "final_assembly_ref": null,
  "rejected_candidates": [],
  "notes": ["Only QUAST evidence is available; BUSCO and contamination_check are pending, so a human should confirm before continuing."],
  "metadata": {
    "evidence_refs": ["/path/to/quast_summary.json"],
    "available_signals": ["quast"],
    "missing_or_pending_signals": ["busco", "contamination_check"]
  }
}
```
