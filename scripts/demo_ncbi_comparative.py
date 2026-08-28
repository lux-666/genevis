"""Quick demo: generate NCBI comparative figures + notebook without a full pipeline run.

Usage:
    conda activate gene_agent
    python scripts/demo_ncbi_comparative.py [species_name]

Output:  /tmp/gene_agent_ncbi_demo/
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.execution.database_resolution import resolve_ncbi_reference_dir
from app.execution.env_resolver import EnvResolver
from app.reference.ncbi import (
    build_related_species_evidence_pack,
    build_related_species_statistics_pack,
)
from app.stages.assembly_review.notebook import (
    build_assembly_review_notebook,
    render_statistics_figures,
    write_notebook,
)

SPECIES = sys.argv[1] if len(sys.argv) > 1 else "卵鳎"
OUT_DIR = Path("/tmp/gene_agent_ncbi_demo")
FIGURES_DIR = OUT_DIR / "figures"
NOTEBOOK_PATH = OUT_DIR / "assembly_review_report.ipynb"
STATS_PACK_PATH = OUT_DIR / "related_species_statistics_pack.json"

# Fake target metrics (what QUAST/BUSCO would normally provide)
TARGET_METRICS = {
    "n50_bp": 1_200_000,
    "assembly_size_bp": 650_000_000,
    "gc_percent": 38.5,
    "busco_complete_percent": 95.2,
    "busco_duplicated_percent": 1.8,
}


def main() -> None:
    print(f"Species: {SPECIES}")
    print(f"Output:  {OUT_DIR}\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reference_dir = resolve_ncbi_reference_dir(EnvResolver())

    print("Building evidence pack...")
    evidence_pack = build_related_species_evidence_pack(
        SPECIES, reference_dir=reference_dir
    )
    print(
        f"  status={evidence_pack['status']}  "
        f"matching_mode={evidence_pack.get('matching_mode')}  "
        f"records={len(evidence_pack.get('selected_records') or [])}"
    )
    if evidence_pack.get("notes"):
        print(f"  notes: {evidence_pack['notes']}")

    print("\nBuilding statistics pack...")
    statistics_pack = build_related_species_statistics_pack(
        SPECIES, reference_dir=reference_dir, target_metrics=TARGET_METRICS
    )
    print(f"  status={statistics_pack['status']}")
    tier_counts = statistics_pack.get("tier_counts", {})
    print(f"  tier counts: {tier_counts}")
    target_pos = statistics_pack.get("target_position", {})
    if target_pos.get("available"):
        print(
            f"  target percentiles: n50={target_pos.get('n50_percentile')}th  "
            f"size={target_pos.get('assembly_size_percentile')}th  "
            f"gc={target_pos.get('gc_percentile')}th  "
            f"busco_complete={target_pos.get('busco_complete_percentile')}th"
        )

    STATS_PACK_PATH.write_text(
        json.dumps(statistics_pack, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nStatistics pack → {STATS_PACK_PATH}")

    print("\nRendering figures...")
    figure_paths = render_statistics_figures(
        statistics_pack, FIGURES_DIR, target_metrics=TARGET_METRICS
    )
    for name, path in figure_paths.items():
        print(f"  {name}.png → {path}")

    figures_relpaths = {
        name: f"figures/{Path(p).name}" for name, p in figure_paths.items()
    }

    print("\nBuilding notebook...")
    notebook = build_assembly_review_notebook(
        statistics_pack=statistics_pack,
        evidence_pack=evidence_pack,
        target_metrics=TARGET_METRICS,
        session_id="demo_session",
        statistics_pack_relpath=str(STATS_PACK_PATH),
        figures_relpaths=figures_relpaths,
    )
    write_notebook(notebook, NOTEBOOK_PATH)
    print(f"Notebook → {NOTEBOOK_PATH}")
    print(f"\nDone. Open with:\n  jupyter notebook {NOTEBOOK_PATH}")


if __name__ == "__main__":
    main()
