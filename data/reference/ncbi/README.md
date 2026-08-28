# NCBI reference snapshots

This page is the sole operator authority for the NCBI reference root and its
installed taxonomy snapshots. Deployment guides link here instead of copying
the contract.

The active `envs.yaml` registers one machine-local root:

```yaml
databases:
  ncbi:
    reference_dir: /home/lux/databases/ncbi
```

Each immediate child directory is one independently verified snapshot. The
initial animal and plant layout is:

```text
databases/ncbi/
├── metazoa/
│   ├── Metazoa.ncbi.json
│   ├── Metazoa.ncbi.lineage
│   ├── Metazoa.ncbi.lineage2
│   └── Metazoa.ncbi.tsv
└── viridiplantae/
    ├── Viridiplantae.ncbi.json
    ├── Viridiplantae.ncbi.lineage
    ├── Viridiplantae.ncbi.lineage2
    └── Viridiplantae.ncbi.tsv
```

The eight files may be placed directly in this layout. Each child must contain
exactly one same-prefix set of `*.ncbi.json`, `*.ncbi.lineage`,
`*.ncbi.lineage2`, and `*.ncbi.tsv`. The JSON export is JSON Lines; do not parse
the complete file as one JSON value. Runtime uses the TSV for assembly records,
the full lineage file for matching, and `lineage2` for ranked taxonomic hints;
all four files are included in the frozen snapshot identity.

Alternatively, install one four-file directory or ZIP through the helper:

```bash
python scripts/install_ncbi_reference.py \
  --reference metazoa \
  --source-dir /shared/reference/metazoa

python scripts/install_ncbi_reference.py \
  --reference viridiplantae \
  --source-url "$GENE_AGENT_NCBI_VIRIDIPLANTAE_SNAPSHOT_URL"
```

The helper writes to `<reference_dir>/<reference>` and validates the four-file
set. Verify either installed snapshot at any time with:

```bash
python scripts/install_ncbi_reference.py --reference metazoa --verify-only
python scripts/install_ncbi_reference.py --reference viridiplantae --verify-only
```

Runtime selection and BUSCO lineage resolution are owned by the
[Assembly Review stage contract](../../../docs/architecture/reference/stage-contracts.md#6-assembly-review).
