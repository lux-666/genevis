# Phase 10 Integration Status

**Updated**: 2026-07-07

## Summary

Phase 10 is mostly integrated for the hifiasm and hifiasm_ont parameter loop.
The catalog, validator, profile helper, knowledge pack, prompt payload builder,
command-builder foundation, parameter recommender node, session/attempt artifact
refs, typed parameter attempts, execution retry gate, review-quality retry graph
edge, attempt-scoped freeze directories, and command/review attempt artifacts
are in place. Planner-registry cleanup, runtime catalog verification, and
focused cleanup regression tests are also in place.

## Completed

1. **Catalog schema and loader**
   - `app/schemas/parameter_catalog.py` defines the three-level catalog model.
   - `app/stages/assembly/parameter_catalog.py` resolves registry methods through descriptor `parameter_catalog`.
   - hifiasm and hifiasm_ont descriptors declare their catalog ids.

2. **Method catalogs**
   - `descriptors/knowledge_packs/assembly/methods/hifiasm/params.yaml`
   - `descriptors/knowledge_packs/assembly/methods/hifiasm_ont/params.yaml`
   - Both catalogs use only L0/L1/L2 and `source_priority` for derivation hints.

3. **Catalog-driven command building**
   - `build_parameter_sources()` derives L0/L1 values from catalog `source_priority`.
   - `build_command()` merges L0/L1/L2 into deterministic argv.
   - Method freeze functions store `parameter_sources` and `argv` in frozen attempt metadata.

4. **Validation and profile helpers**
   - `validate_recommended_parameters()` validates L2-only recommendations, catalog constraints, and hard profile-backed rules.
   - `build_full_parameter_profile()` and `build_prompt_profile()` exist.
   - Full parameter profiles are written as attempt-scoped replay artifacts.

5. **Structured failure paths**
   - Missing catalogs and malformed recommended-parameter state return structured failure updates from freeze nodes.

6. **Parameter recommender knowledge pack** ✅
   - `descriptors/knowledge_packs/assembly/parameter_recommender/` now uses manifest files plus `cases/*.yaml` and `rules/*.yaml` shards
   - `SKILL.md` carries pack metadata and explainable heuristics only
   - Case shards store evidence observations, not few-shot recommendation answers
   - Rule shards store hard validator boundaries only
   - All observed/constraint parameter keys are aligned with real hifiasm catalog L2 parameters

7. **Prompt payload builder** ✅
   - `app/stages/assembly/prompt_payload.py` implemented
   - `build_prompt_payload()` creates task-scoped prompt profiles
   - Prompt payload includes selected evidence cases plus selection metadata, not the full case database
   - Main-flow artifact writes use `data/artifacts/assembly/<session>/parameter_attempt_<N>/prompt_payload.json`
   - Hashes are integrity fields, not runtime refs

8. **Parameter recommender node** ✅
   - `recommend_assembly_parameters()` writes profile, prompt, response, and validation artifacts.
   - The node appends compact typed `parameter_attempts`.
   - Invalid L0/L1/L2 recommendations stop before freeze/run.
   - Route selection and parameter recommendation both use the shared assembly decision boundary.

9. **Execution retry gate** ✅
   - Command failures can store the last 100 log lines and re-enter parameter recommendation.
   - Retry attempts increment `current_parameter_attempt_id`.
   - hifiasm/hifiasm_ont freeze paths use attempt-scoped work/artifact directories.

10. **Review-quality retry gate** ✅
   - assembly_review failures can set a parameter replan request with failure
     diagnosis.
   - The next recommendation receives prior attempt context and review metrics.
   - Retry caps prevent infinite review loops.

11. **Attempt artifact completion** ✅
   - hifiasm/hifiasm_ont freeze nodes write `command.json` under the current
     parameter attempt directory and store `command_ref` on the attempt.
   - Review-quality failures write `review_failure.json` under the reviewed
     parameter attempt directory and store `review_failure_ref` on the attempt.

12. **Documentation package** ✅
   - `docs/methodology/parameter_recommendation_closed_loop.md`
   - `docs/implementation/parameter_recommendation_loop.md`
   - `docs/user_guide/parameter_recommendation.md`
   - `docs/phases/phase10/replay_contract.md`
   - `docs/phases/phase10/no_compat_cleanup_note.md`
   - Docs describe the descriptor-derived planner catalog, verified hifiasm
     runtime help surface, replay contract, and no-compat cleanup.

13. **No-compat planner cleanup** ✅
   - `app/entrypoints/planner_catalog.py` derives planner method metadata from
     `DescriptorRegistry`.
   - Method descriptors carry planner-facing required parameters, defaults,
     selection roles, display names, and planner-only method markers.
   - `ask_planner.py` no longer maintains `_STAGE_METHODS`.
   - `ask_interactive.py` consumes the same descriptor-derived catalog.
   - Registry executable invariant tests use stage-owned runner declarations
     and ignore descriptor-declared planner-only methods.

14. **Runtime catalog verification** ✅
   - hifiasm and hifiasm_ont catalogs were verified against local hifiasm
     `0.25.0-r726` help output through a skip-safe test that runs when
     `HIFIASM_BIN` or `hifiasm` on `PATH` is available.
   - Evidence is recorded in
     `docs/phases/phase10/hifiasm_runtime_verification_2026-07-07.md`.
   - Release verification can set `PHASE10_REQUIRE_HIFIASM_RUNTIME=1` so a
     missing hifiasm binary fails instead of skipping.

15. **ONT retry proof** ✅
   - Review-failure retry is covered by a regression proving a second
     hifiasm_ont recommendation can adjust `purge_level`, `--rl-cut`, and
     `--sc-cut`, and that those L2 values map into deterministic argv.

## Pending

1. **Broader closed-loop coverage**
   - The focused tests cover the main contracts; a larger end-to-end closed-loop
     test with mock execution/review can still strengthen confidence.

## Next Recommended Slice

**Next: closed-loop test hardening**

1. Add or extend closed-loop tests around max retry behavior and attempt
   artifact refs.
2. Keep binary-backed catalog verification current when the supported hifiasm
   version changes.
