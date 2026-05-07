# 01 Natural-Language Entry Parser

Task status: planned.

## Goal

Add a thin natural-language request parser outside LangGraph so users can start
the existing genome workflow without manually writing CLI arguments.

Target flow:

```text
natural language
-> intent parser LLM
-> EntryRequest
-> existing structured run path
-> initial_state(...)
-> build_graph().invoke(...)
```

## Project Context

The current entrypoint is `app/entrypoints/run.py`:

```text
_parse_args(...)
-> _run_session(args)
-> initial_state(session_id, input_manifest_ref, data_dir)
-> build_graph().invoke(...)
```

`app/llm/config.py`, `app/llm/factory.py`, and `app/llm/prompts.py` exist but
are placeholders. The reference project keeps the natural-language parser in
`app/entrypoints/intent.py` and reuses the structured graph entry instead of
adding a parser node to LangGraph.

## Files

- Create: `app/entrypoints/intent.py`
- Modify: `app/entrypoints/run.py`
- Modify: `app/llm/config.py`
- Modify: `app/llm/factory.py`
- Modify: `app/llm/prompts.py`
- Create: `app/llm/prompt_bundles/prompts/intent/parse_system.md`
- Create: `app/llm/prompt_bundles/prompts/intent/parse_user.md`
- Test: `tests/test_intent.py`
- Test: `tests/test_entrypoints.py`

## Design

### Entry Request

Create one narrow Pydantic model that represents only graph-entry facts:

```python
class EntryRequest(BaseModel):
    command: str = "run"
    session_id: str
    manifest: str
    data_dir: str = "data"

    def to_cli_argv(self) -> list[str]:
        ...
```

Do not add stage routing, method preferences, thresholds, database choices, or
review decisions to this first request model. Those are not stable enough yet.

### Parser Boundary

`parse_request_with_llm(request_text, *, defaults=None, model=None)` should:

- render an intent prompt bundle;
- bind structured JSON output when the provider supports it;
- parse exactly one JSON object;
- merge explicit defaults such as `session_id` or `data_dir`;
- validate the result as `EntryRequest`;
- fail before graph invocation when required fields are missing.

The parser may infer a manifest path from natural language as a convenience, but
the validated request is still the only thing passed to the graph.

### CLI Reuse

Add a user-facing natural-language command only as an adapter, for example:

```text
gene-agent ask "run session s1 with manifest inputs/manifest.yaml"
```

The `ask` command should produce `EntryRequest`, convert it to the existing
structured run path, and invoke the same graph entry logic as `gene-agent run`.
Structured callers must still be able to call `run` without any LLM dependency.

### LLM Layer

Implement `app/llm/` as provider plumbing only:

- `config.py`: profile loading from environment variables;
- `factory.py`: construct an OpenAI-compatible chat model;
- `prompts.py`: load prompt bundles and render provider-neutral messages.

`app/llm/` must not know about graph routing, stage behavior, descriptor
selection, review decisions, or ledger writes.

### YAML Boundary

Do not create a new YAML file for intent schema, parsed request fields, session
facts, or CLI defaults. The request schema belongs in code and tests. Prompt
text belongs in prompt bundles. Existing `config/defaults.yaml` should not
become a second request model.

## Tasks

- [ ] Add `EntryRequest` with `to_cli_argv()` for the existing `run` command.
- [ ] Add JSON extraction and schema validation for intent parser responses.
- [ ] Add LLM profile loading and chat-model factory behind `app/llm/`.
- [ ] Add intent prompt bundle files for genome workflow entry parsing.
- [ ] Add `gene-agent ask` as a thin adapter that reuses the structured run
  path.
- [ ] Add tests that malformed LLM output fails before graph invocation.
- [ ] Add tests that structured `run` still works without LLM dependencies.
- [ ] Add tests that `ask` parses natural language, builds `EntryRequest`, and
  invokes the graph with the same `initial_state(...)` contract.

## Acceptance

- Natural language can start the existing structured graph path.
- The graph never receives raw natural language.
- LLM parsing is optional and bypassable.
- `--stages` remains unexposed until requested-stage semantics are approved.
- Parser output contains only stable graph-entry fields.
- No new YAML layer duplicates CLI args, state fields, or method catalogs.

## Non-Goals

- Do not implement requested-stage routing.
- Do not let the LLM select bioinformatics methods.
- Do not let the LLM approve review decisions or thresholds.
- Do not add LLM nodes inside the parent graph.
- Do not add a production API server.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_intent tests.test_entrypoints -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
