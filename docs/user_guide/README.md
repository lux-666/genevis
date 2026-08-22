# User Guide Index

This page routes operators who need to install Gene Agent, prepare a run,
monitor it, review a gate, or recover a durable session. It is an index only;
the copyable examples and the linked operator guides own the user procedure.

## Main routes

| Task | Start here | Follow when needed |
|---|---|---|
| New host or missing tool environment | [New Host Setup](../deployment/migration.md) | [Run and Recovery](run-and-recovery.md) after host checks succeed |
| Mac checkout to server checkout | [Mac Development and Server Execution](../deployment/mac_dev_server_pull.md) | [Run and Recovery](run-and-recovery.md) for the actual session |
| Run contig from raw reads | [Run and Recovery](run-and-recovery.md) | [Contig manifest](../examples/contig_manifest.template.yaml) and [contig RunConfig](../examples/contig_run_config.template.json) |
| Review an existing assembly | [Existing-assembly manifest](../examples/existing_assembly_manifest.template.yaml) | [Assembly-review RunConfig](../examples/assembly_review_run_config.template.json), then [Run and Recovery](run-and-recovery.md) |
| Understand or decide LLM parameters | [Parameter Recommendation](parameter_recommendation.md) | [Parameter contract](../architecture/reference/parameter-recommendation.md) only when exact fields or persistence rules matter |
| Recover a blocked or interrupted session | [Run and Recovery](run-and-recovery.md) | Use its `review` or decision-file `resume` route; do not start a second workflow manually |
| Exercise repeated stage occurrences | [Repeated-stage RunConfig](../examples/repeated_stage_occurrences_run_config.template.json) | [Run and Recovery](run-and-recovery.md) for inspection and recovery |

## Recommended reading order

For a fresh server:

```text
New Host Setup
  -> Mac Development and Server Execution (if code was just pulled)
  -> Run and Recovery
  -> examples/
```

For an already prepared checkout:

```text
Run and Recovery
  -> the paired example files for the chosen entry mode
  -> Parameter Recommendation when an approval gate appears
```

The five-stage raw-read contig example is the authoritative copyable example
for that entry mode. Existing-assembly review and repeated-stage examples are
separate use cases, not alternate versions of the contig template.

## Boundary with architecture references

Operator guides explain what to do. The architecture references define exact
contracts for freeze, progress, resources, execution evidence, review, and
resume. If a guide and a reference appear to disagree, follow the reference
owner and report the documentation drift rather than creating a local variant.
