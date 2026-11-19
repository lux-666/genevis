# Contributing to Gene Agent

Gene Agent is at alpha stage. Bug reports, installation feedback, documentation
improvements, and reproducible workflow failures are useful contributions.
English and Chinese reports are welcome.

## Report a problem

Open a [bug report](https://github.com/lux-666/genevis/issues/new?template=bug_report.yml)
with your Git commit, Python/OS versions, the affected stage or command, expected
behavior, and a minimal reproduction. For execution failures, include the relevant
tool versions and a short, redacted error excerpt. State whether the problem
occurs in an offline code test or a real biological-tool run.

Use synthetic inputs or public data with a source link and checksum. Remove API
keys, private sample identifiers, and host credentials before attaching logs or
configuration. Do not upload your `.env`, machine-local environment registry,
large sequencing files, or a complete session directory to an issue.

## Set up development

From a checkout, use Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints/ci.txt -e '.[dev,tui]'
```

The [offline walkthrough](docs/user_guide/offline-verification.md) is a small
first check. During local development, run the pytest files or test cases that
cover the affected behavior. For example, a change to Assembly method review
can be checked with the following command.

```bash
python -m pytest -q tests/test_assembly_route_contracts.py
```

CI and release verification run the full repository gate.

```bash
bash scripts/server_test.sh
```

The gate checks formatting, lint, types, offline regression, compilation, and
the diff. Report which checks you ran. Real tool installation and acceptance
follow the [operator guide](docs/user_guide/run-and-recovery.md).

## Submit a change

Keep pull requests focused on a specific problem. Explain the trigger, resulting
behavior, checks run, and any remaining limitation. Add a regression test for a
behavioral fix and update the affected documentation. Describe input provenance
and check that you can redistribute any contributed code or data.

The [developer guide](docs/developer_guide/README.md) links the relevant contracts
and implementation owners. The [engineering policy](docs/architecture/reference/governance.md)
owns change control and quality gates; this page is a contributor entry point.

## Try the software on real data

A useful trial records the commit, public input accession/source and checksums,
tool/database versions, resource limits, commands, review decisions, final output
checksums, and quality metrics. Record failures and recovery attempts too. Share
a small redacted summary through an issue, with permission to publish it. Follow
the existing acceptance order in the operator guide before attempting a full
workflow. There is no need to run every supported stage for a useful report.
