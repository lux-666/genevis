# 10 Ask Staged Clarification

Task status: in-progress.

## Goal

Make `gene-agent ask` a true staged operator planning entry: instead of
silently falling back to planned-only execution, ask should collect stage
selection, method choices, and required parameters through explicit
clarification steps, then confirm the execution plan before starting.

## Scope

- First version covers the assembly/Flye end-to-end path only.
- Stages without real method_decision consumption (genome_survey,
  gene_annotation, function_annotation) are not exposed for method selection.
- Final confirm step required before any graph invocation.
- `--run-config` remains a direct bypass for operators who provide their own
  configuration.

## Follow-up items

- ASK_METHOD_CAPABILITIES bridge: move `execution_support`, `required_parameters`,
  and `parameter_defaults` into MethodDescriptor / descriptor YAML so ask planner
  reads from a single source of truth instead of maintaining a parallel registry.
- genome_survey parameters: keep method-specific (jellyfish_parameters,
  genomescope2_parameters) rather than a shared survey_parameters model, even if
  current methods happen to share fields.
- planned-only stages: show them as planned-only in clarification output; do not
  let users assume method_selections controls execution when the stage does not
  consume them.
- descriptor CWD: all stage nodes use `Path("descriptors/registry.yaml")` which
  assumes repo root as CWD; centralize or make configurable.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_ask_plan tests.test_entrypoints tests.test_intent tests.test_run_config -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
