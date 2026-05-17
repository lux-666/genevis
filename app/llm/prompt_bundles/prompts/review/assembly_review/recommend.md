Read the review request JSON and recommend one provisional assembly review decision.

Rules:
- Return only a JSON object.
- Use only decisions listed in `decisions_allowed`.
- Prefer `stop_human_review` if the evidence is insufficient or unsafe to decide.
- Do not invent biological thresholds.
- Do not include `decided_by` or `rationale_artifact_ref`; the workflow will add those after writing this recommendation artifact.
- Include concise rationale in `notes` and any input artifact references in `metadata.evidence_refs` when available.
