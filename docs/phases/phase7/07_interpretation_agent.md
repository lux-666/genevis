# 07-07 Interpretation Agent (Direction Note)

Task status: direction only; out of scope until the annotation agent lands.

## Goal (future)

A third virtual agent that consumes the assembly + annotation outputs and
produces a biological interpretation / synthesis — turning artifacts (masked
genome, gene set, function tables, BUSCO, repeat content) into a coherent
narrative an operator can read, rather than a pile of files.

## Why it is a note, not a task yet

- No stage exists for this; it is a wholly new terminal capability, not a
  retrofit onto `function_annotation`.
- It depends on the annotation agent producing real, reviewed outputs first
  (tasks 01-06). Interpreting planning-only placeholders has no value.

## Rough Shape (to be designed later)

- Likely a new terminal stage after `function_annotation`, owning its own
  review gate (does the interpretation hold up?).
- Consumes durable artifacts + the run trace; produces an interpretation
  artifact (and possibly a human-readable report) registered like other outputs.
- Should not invent biological conclusions not supported by the underlying
  artifacts/evidence — same discipline as the run trace's "surface gaps, don't
  fabricate" rule.

## Open Questions (defer)

- Where the agent boundary sits and whether it stays implicit over `STAGE_ORDER`.
- How much is LLM synthesis vs deterministic summarization.
- What "review" means for an interpretation (accept / request revision / reject).
