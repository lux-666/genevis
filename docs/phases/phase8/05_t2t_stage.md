# 08-05 T2T Stage

任务状态：待实施。T2T 是 Phase 8 新增的 top-level stage，建议 stage name 为 `t2t`。它位于 `hic_scaffolding` 之后、`repeat_annotation` 之前。

## 目标

`t2t` stage 对最终候选 assembly 做 telomere/gap/readiness review，并在 near/strict T2T 目标下生成可执行的人工 review tasks 和 final gate。

MVP 不自动修改 genome，不做 telomere extension，不把 TeloComp/Teloclip 结果写回 FASTA。

## Current Implementation Compatibility

Commit `ca685a9` added assembly-scoped T2T prototype schemas:

- `AssemblyT2TReadiness`
- `AssemblyReviewTasks`
- artifact constants `assembly.t2t_readiness` and `assembly.review_tasks`
- knowledge packs under `descriptors/knowledge_packs/assembly/t2t_criteria` and `assembly/review_task_guide`

These are not a conflict. They are pre-stage contracts from the earlier assembly-internal design. When the top-level `t2t` stage is added, keep them readable while introducing `t2t.*` artifacts and stage-owned output keys. Migration can be one-way:

1. `t2t` consumes existing assembly-review / hic-scaffolding final refs.
2. `t2t` writes new `stage_outputs.t2t` and `t2t.*` artifacts.
3. Any older `assembly.t2t_readiness` refs are treated as legacy evidence, not the canonical final T2T decision.

## Inputs

`t2t` 按优先级寻找输入 assembly：

1. `stage_outputs.hic_scaffolding.final_assembly_for_t2t_or_annotation`
2. `stage_outputs.hic_scaffolding.final_assembly_for_annotation`
3. `stage_outputs.assembly_review.final_assembly_for_scaffolding_or_annotation`
4. `stage_outputs.assembly_review.final_assembly_ref`

Required context:

```python
{
    "input_assembly_ref": "...",
    "target_level": "near_t2t | strict_t2t",
    "source_stage": "hic_scaffolding | assembly_review",
    "source_candidate_provenance": {...},
}
```

If no assembly ref exists, fail or block with a clear missing-input reason.

## Boundary

`t2t` 负责：

- telomere motif discovery or expected motif handling。
- terminal-end telomere check。
- gap survey。
- T2T readiness summary。
- review task generation。
- final human gate for strict T2T。
- setting final assembly ref for downstream annotation when accepted.

`t2t` 不负责：

- long-read assembly。
- Hi-C scaffolding or manual curation。
- polishing/correction method execution。
- automatic genome modification。

## Stage Nodes

```text
enter_t2t
  -> resolve_t2t_input
  -> run_telomere_motif_discovery
  -> run_terminal_telomere_check
  -> run_gap_survey
  -> generate_t2t_readiness
  -> generate_t2t_review_tasks
  -> t2t_final_gate
       completed       -> commit
       blocked_human   -> block
       failed          -> fail
```

Tool failures in motif discovery should record lower confidence and continue. Missing input assembly should fail/block.

## MVP Tools

| Check | MVP behavior |
|---|---|
| motif discovery | optional `tidk`; unavailable records note |
| terminal telomere check | Python FASTA-end parser |
| gap survey | Python parser or `seqkit locate -p "N+"` if available |
| readiness synthesis | deterministic safety layer plus optional LLM |
| completion planning | notes/tasks only, no FASTA modification |

## Readiness

Readiness artifact:

```python
{
    "stage": "t2t",
    "input_assembly_ref": "...",
    "target_level": "near_t2t | strict_t2t",
    "verdict": "not_t2t | near_t2t | strict_t2t | insufficient",
    "confidence": 0.0,
    "reasoning": "...",
    "telomere_summary_ref": "...",
    "gap_survey_ref": "...",
    "source_stage": "hic_scaffolding | assembly_review",
}
```

Python safety layer must prevent impossible claims, for example:

- strict T2T with no telomere evidence.
- strict T2T with unreviewed gaps.
- near/strict T2T when input assembly ref is missing.

## Final Gate

Strict T2T always blocks for human confirmation.

Allowed decisions:

```python
(
    "adopt_current",
    "adopt_near_t2t",
    "adopt_strict_t2t",
    "continue_low_confidence",
    "request_more_data",
    "stop_human_review",
)
```

On accepted decisions, `t2t` writes:

```python
{
    "status": "completed",
    "final_assembly_for_annotation": input_assembly_ref,
    "t2t_readiness_ref": "...",
    "t2t_review_tasks_ref": "...",
    "decision_ref": "...",
}
```

`request_more_data` keeps the workflow blocked and does not change downstream final assembly refs.

## Review Gate Declaration

```python
register_review_gate(ReviewGateDeclaration(
    stage="t2t",
    gate_id="t2t_final_review",
    decisions_allowed=(
        "adopt_current",
        "adopt_near_t2t",
        "adopt_strict_t2t",
        "continue_low_confidence",
        "request_more_data",
        "stop_human_review",
    ),
    decision_applier="app.stages.t2t.nodes:apply_t2t_final_review",
    prompt_bundle="t2t.final_review",
    prompt_adapter="generic",
    template_preferred_decision="adopt_near_t2t",
))
```

## Tests

- `test_t2t_stage_resolves_hic_scaffolding_final_ref_first`
- `test_t2t_stage_falls_back_to_assembly_review_final_ref`
- `test_t2t_missing_assembly_ref_fails_or_blocks`
- `test_t2t_motif_unavailable_continues_low_confidence`
- `test_t2t_gap_survey_records_gaps`
- `test_t2t_strict_target_always_blocks_final_gate`
- `test_t2t_accept_decision_sets_final_assembly_for_annotation`
- `test_t2t_does_not_modify_input_fasta`

## Acceptance

- `t2t` is a top-level stage in `STAGE_ORDER`.
- It consumes existing stage outputs, not assembly-internal state.
- It writes `stage_outputs.t2t`.
- It never auto-modifies genome FASTA in Phase 8.
