# 01 Entry Interaction

Task status: implemented.

## Goal

Define how users start a workflow, including a thin natural-language request
parser outside LangGraph. The entry interaction should make workflow startup
ergonomic without changing workflow decision authority.

Target flow:

```text
start / ask
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

Task 01 is the first half of the Phase 3 interaction loop:

```text
start / ask
  -> run graph
  -> maybe blocked_human
```

Task 02 owns the second half:

```text
blocked_human
  -> inspect review request
  -> human decides
  -> resume
```

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

### User Interaction

Support two equivalent workflow-start surfaces:

```text
gene-agent run --manifest inputs/manifest.yaml --data-dir data
gene-agent run --session-id s1 --manifest inputs/manifest.yaml --data-dir data
gene-agent ask "use inputs/manifest.yaml to start a workflow"
gene-agent ask "use inputs/manifest.yaml to start session s1"
```

Both must produce the same structured graph entry facts. The `ask` path is an
adapter; it should not have its own workflow semantics. When `session_id` is
omitted, the entrypoint generates the next daily id from existing ledger session
directories: `gene-YYYYMMDD-NNNNNN`.

For example, this user request:

```text
用 inputs/manifest.yaml 开一个 s1
```

should become:

```json
{
  "command": "run",
  "session_id": "s1",
  "manifest": "inputs/manifest.yaml",
  "data_dir": "data"
}
```

Then the existing structured path should call:

```text
initial_state(session_id="s1", input_manifest_ref="inputs/manifest.yaml", data_dir="data")
build_graph().invoke(..., config={"configurable": {"thread_id": "s1"}})
```

### Entry Request

Create one narrow Pydantic model that represents only graph-entry facts:

```python
class EntryRequest(BaseModel):
    command: str = "run"
    session_id: str | None = None
    manifest: str
    data_dir: str = "data"

    def to_cli_argv(self) -> list[str]:
        ...
```

Do not add stage routing, method preferences, thresholds, database choices, or
review decisions to this first request model. Those are not stable enough yet.

`session_id` is the workflow run primary key, not a stage selector. It should
identify the run and checkpoint thread; it should not imply where to restart,
which stage to run, or whether a previous stage should be rerun. When omitted,
the system assigns the next `gene-YYYYMMDD-NNNNNN` id from the existing ledger
session directories for that date.

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

If a user asks to continue, inspect, retry, rerun, or rebuild an existing
session, task 01 should not silently reinterpret that as a new `run`. Those
phrases belong to lifecycle/status commands in task 02 or later lifecycle
tasks.

### Session Creation And Persistence

On start, the system should make the session observable by `session_id`:

- create or reuse the ledger session metadata under `data/ledger/{session_id}`;
- use `thread_id=session_id` for graph checkpoint identity;
- return a compact run summary with `session_id`, `current_stage`,
  `supervision_state`, `active_interrupt` when present, and key artifact refs;
- make clear when the current scaffold only has memory checkpoints and therefore
  durable cross-process resume is not yet available.

If a durable session runtime file is added later, it should summarize state for
inspection only. It must not become a second source of truth for graph routing.

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

- [x] Add `EntryRequest` with `to_cli_argv()` for the existing `run` command.
- [x] Add JSON extraction and schema validation for intent parser responses.
- [x] Add LLM profile loading and chat-model factory behind `app/llm/`.
- [x] Add intent prompt bundle files for genome workflow entry parsing.
- [x] Add `gene-agent ask` as a thin adapter that reuses the structured run
  path.
- [x] Return a compact run summary after both `run` and `ask`.
- [x] Reject natural-language lifecycle requests such as continue/resume/rerun
  from the start parser instead of treating them as new workflow starts.
- [x] Add tests that malformed LLM output fails before graph invocation.
- [x] Add tests that structured `run` still works without LLM dependencies.
- [x] Add tests that `ask` parses natural language, builds `EntryRequest`, and
  invokes the graph with the same `initial_state(...)` contract.

## Implementation Notes

改动要点：

- Added `app/entrypoints/intent.py` with a narrow `EntryRequest` contract,
  optional `session_id`, non-empty `manifest` / `data_dir` validation,
  path-separator rejection for explicit `session_id`, strict extra-field
  rejection, single-object JSON extraction, lifecycle-request rejection, and
  LLM-backed parsing outside LangGraph.
- Extended `app/entrypoints/run.py` with the minimum `ask` adapter. `ask` parses
  natural language, converts the result through `EntryRequest.to_cli_argv()`,
  reparses the existing structured `run` command, and reuses `_run_session(...)`.
  Both `run` and `ask` generate `gene-YYYYMMDD-NNNNNN` when `session_id` is
  omitted; explicit ids remain supported.
- Kept `--stages` unexposed and rejected lifecycle-like requests such as resume,
  inspect, retry, rerun, and rebuild before graph invocation.
- Implemented `app/llm/config.py`, `app/llm/factory.py`, and
  `app/llm/prompts.py` as provider plumbing only. Runtime configuration uses
  only `GENE_AGENT_LLM_*`; it does not fall back to `BIOAGENT_*`.
- Added prompt bundle files under
  `app/llm/prompt_bundles/prompts/intent/` for the entry parser.
- Added ledger session creation at structured run start via
  `Ledger(data_dir).create_session(...)`; this is observability only and not a
  durable resume/checkpoint format. The run summary explicitly reports
  `checkpoint_persistence: memory_only` and warns that durable cross-process
  resume is not available yet.
- Added `tests/test_intent.py` and extended `tests/test_entrypoints.py` for
  parser validation, LLM fake-model behavior, Gene Agent-specific env vars,
  local `.env` loading, structured `run`, and `ask` reuse of the existing graph
  entry contract.
- Created a local ignored `.env` by adapting values from the sibling
  `rna_agent/.env` into `GENE_AGENT_*` names without printing secrets or adding
  a committed config source. `_load_dotenv_without_override()` is intentionally a
  narrow standard-library loader for simple `KEY=value` lines, not a full dotenv
  grammar implementation.

Verified focused command:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_intent tests.test_entrypoints -v
```

Result: 32 tests passed.

## Acceptance

- Natural language can start the existing structured graph path.
- The graph never receives raw natural language.
- LLM parsing is optional and bypassable.
- `session_id` identifies the workflow run and graph thread but does not encode
  stage, method, threshold, or review decisions; it is generated by default and
  may still be explicitly provided.
- `--stages` remains unexposed until requested-stage semantics are approved.
- Parser output contains only stable graph-entry fields.
- `ask` cannot accidentally restart a blocked session when the user intended to
  continue or inspect it.
- No new YAML layer duplicates CLI args, state fields, or method catalogs.

## Non-Goals

- Do not implement requested-stage routing.
- Do not let the LLM select bioinformatics methods.
- Do not let the LLM approve review decisions or thresholds.
- Do not add LLM nodes inside the parent graph.
- Do not implement status/inspect/resume in task 01 except for returning the
  immediate run summary.
- Do not add a production API server.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_intent tests.test_entrypoints -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
