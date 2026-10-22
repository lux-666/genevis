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
first check. Before submitting a code change, run the repository gate:

```bash
bash scripts/server_test.sh
```

This runs format, lint, type, regression, compilation, and diff checks. It excludes
tests marked `real_tool`. Real tool installation and acceptance follow the
[operator guide](docs/user_guide/run-and-recovery.md); state which of those checks
you actually ran. A code test pass does not prove an assembly result is valid.

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
