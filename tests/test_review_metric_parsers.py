"""Final report validity is independent of biological quality thresholds."""

from pathlib import Path

import pytest

from app.stages.assembly_review.method_application.busco import (
    parse_busco_short_summary,
)
from app.stages.assembly_review.method_application.quast import parse_quast_report_tsv
from app.storage.paths import DataLayoutError


@pytest.mark.parametrize(
    "content",
    [
        "not a report\n",
        "Assembly\ta\tb\n# contigs\t1\t1\nTotal length\t4\t4\n",
        "Assembly\ta\n# contigs\t1\nTotal length\n",
        "Assembly\ta\n# contigs\t1\n# contigs\t2\nTotal length\t4\n",
        "Assembly\ta\nN50\t4\n",
        "Assembly\ta\n# contigs\tNA\nTotal length\t4\n",
        *[
            f"Assembly\ta\n# contigs\t1\nTotal length\t{value}\n"
            for value in ("invalid", "NaN", "inf", "-1", "1.5")
        ],
    ],
)
def test_quast_rejects_malformed_metrics(tmp_path: Path, content: str) -> None:
    path = tmp_path / "report.tsv"
    path.write_text(content)
    with pytest.raises(DataLayoutError):
        parse_quast_report_tsv(path)


def test_quast_preserves_optional_missing_values(tmp_path: Path) -> None:
    path = tmp_path / "report.tsv"
    path.write_text(
        "Assembly\ta\n# contigs\t2\nTotal length\t1,000\n"
        "# misassemblies\t-\nGenome fraction (%)\tNA\nNGA50\tN/A\n"
    )
    metrics = parse_quast_report_tsv(path)
    assert metrics["total_length"] == 1000
    assert metrics["misassemblies_raw"] == "-"
    assert metrics["genome_fraction_percent_raw"] == "NA"
    assert metrics["nga50_raw"] == "N/A"
    assert "misassemblies" not in metrics


_LOW_BUSCO = (
    "C:5.0%[S:4.0%,D:1.0%],F:10.0%,M:85.0%,n:100\n"
    "5 Complete BUSCOs (C)\n"
    "4 Complete and single-copy BUSCOs (S)\n"
    "1 Complete and duplicated BUSCOs (D)\n"
    "10 Fragmented BUSCOs (F)\n"
    "85 Missing BUSCOs (M)\n"
    "100 Total BUSCO groups searched\n"
)


def test_busco_accepts_low_but_valid_completeness(tmp_path: Path) -> None:
    path = tmp_path / "short_summary.txt"
    path.write_text(_LOW_BUSCO)
    metrics = parse_busco_short_summary(path)
    assert metrics["complete_percent"] == 5.0
    assert metrics["missing_buscos"] == 85


@pytest.mark.parametrize(
    "content",
    [
        "not a report\n",
        _LOW_BUSCO.replace("C:5.0%", "C:..%"),
        _LOW_BUSCO.replace("C:5.0%", "C:NaN%"),
        _LOW_BUSCO.replace("C:5.0%", "C:101%"),
        _LOW_BUSCO.replace("C:5.0%", "C:-1%"),
        _LOW_BUSCO.replace("100 Total BUSCO groups searched\n", ""),
        _LOW_BUSCO.replace("5 Complete BUSCOs", "6 Complete BUSCOs"),
        _LOW_BUSCO.replace("85 Missing BUSCOs", "84 Missing BUSCOs"),
    ],
)
def test_busco_rejects_malformed_metrics(tmp_path: Path, content: str) -> None:
    path = tmp_path / "short_summary.txt"
    path.write_text(content)
    with pytest.raises(DataLayoutError):
        parse_busco_short_summary(path)


@pytest.mark.parametrize("parser", [parse_busco_short_summary, parse_quast_report_tsv])
def test_reports_reject_corrupt_text(tmp_path: Path, parser) -> None:
    path = tmp_path / "report"
    path.write_bytes(b"\xff\xfe\n")
    with pytest.raises(DataLayoutError):
        parser(path)
