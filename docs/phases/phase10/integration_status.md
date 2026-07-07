# Phase 10 Integration Status

**Updated**: 2026-07-07

## Summary

Phase 10 is mostly integrated for the hifiasm and hifiasm_ont parameter loop.
The catalog, validator, profile helper, knowledge pack, prompt payload builder,
command-builder foundation, parameter recommender node, session/attempt artifact
refs, typed parameter attempts, execution retry gate, review-quality retry graph
edge, and attempt-scoped freeze directories are in place. Planner-registry
cleanup, runtime catalog verification, standalone command/review artifacts, and
final documentation are still pending.

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

## Pending

1. **Attempt artifact completion**
   - `parameter_attempts` record recommendation/freeze/run/review data, but
     command metadata and review failure context should also be written as
     attempt-scoped artifacts if the final replay contract keeps `command.json`
     and `review_failure.json` as standalone files.

2. **Runtime catalog verification**
   - Verify hifiasm and hifiasm_ont catalogs against the project-pinned hifiasm binary/help output in the execution environment.

3. **No-compat architecture cleanup**
   - Derive planner method metadata from descriptors/catalogs.
   - Delete the planner's duplicate `_STAGE_METHODS` registry.
   - Delete old compatibility paths rather than preserving wrappers.

4. **Final documentation**
   - Write the methodology, implementation guide, user guide, replay contract,
     and no-compat cleanup summary.

## Next Recommended Slice

**Next: planner no-compat cleanup**

1. Replace `ask_planner.py` `_STAGE_METHODS` with descriptor/catalog-derived
   planner metadata.
2. Update `ask_interactive.py` and registry invariant tests to consume the same
   derived metadata.
3. Add architecture-regression tests that fail if `_STAGE_METHODS`, old
   planner constants, stage-local assembly model invocation, or hash-only
   runtime refs return.
4. After planner cleanup, add the remaining command/review attempt artifacts and
   run hifiasm binary/help catalog verification.
