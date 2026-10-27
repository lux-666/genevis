"""Validation shared by the standalone recovery and its regression tests."""

from __future__ import annotations

import hashlib
import json
import warnings
from collections import Counter
from pathlib import Path


def sha256(path):
    with Path(path).open("rb") as handle:
        # Python 3.9 has no hashlib.file_digest.
        result = hashlib.sha256()
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def assembly_info(path):
    lengths, placements, groups = {}, [], []
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        if line.startswith(">"):
            ident, length = int(fields[1]), int(fields[2])
            if ident <= 0 or ident in lengths or length <= 0:
                raise ValueError("Duplicate fragment id or nonpositive length")
            lengths[ident] = length
        else:
            group = [abs(int(value)) for value in fields]
            placements.extend(group)
            groups.append(group)
    if (
        not lengths
        or not groups
        or len(placements) != len(set(placements))
        or set(placements) != set(lengths)
    ):
        raise ValueError("Missing, duplicate or unknown assembly placements")
    return dict(
        total_bp=sum(lengths.values()),
        fragments=len(lengths),
        groups=len(groups),
        main_group_bp=sum(lengths[value] for value in groups[0]),
    )


def validate_images(directory, total_bp, resolutions):
    """Validate image files and warn about unexpected diagonal window coverage."""
    directory = Path(directory)
    rows = [
        json.loads(line)
        for line in (directory / "info.txt").read_text().splitlines()
        if line.strip()
    ]
    expected = Counter()
    for resolution in resolutions:
        if resolution < 500:
            continue
        width = min(total_bp, resolution * 700)
        for start in range(0, total_bp, resolution * 400):
            end = min(start + width, total_bp)
            start = end - width
            # Upstream may render the same end window more than once.
            expected[(resolution, "assembly", start, end, "assembly", start, end)] += 1
    if any(len(row) != 1 for row in rows):
        raise ValueError("Official inference requires exactly one image per index row")
    fields = (
        "resolution",
        "chr_A",
        "chr_A_start",
        "chr_A_end",
        "chr_B",
        "chr_B_start",
        "chr_B_end",
    )
    observed = Counter(
        tuple(meta.get(field) for field in fields)
        for row in rows
        for meta in row.values()
    )
    names = [Path(name) for row in rows for name in row]
    if not names or len(names) != len(set(names)):
        raise ValueError("Empty image set or duplicate image paths")
    if not all(
        name.is_absolute() and name.is_file() and name.stat().st_size for name in names
    ):
        raise ValueError("Image index must refer to nonempty files by absolute path")
    if observed != expected:
        warnings.warn(
            "Image coverage differs from official diagonal windows; "
            f"continuing with {len(names)} indexed images",
            RuntimeWarning,
            stacklevel=2,
        )
    return len(names)


def available_disk(path, minimum_gib):
    import shutil

    free = shutil.disk_usage(path).free
    if free < minimum_gib * 2**30:
        raise RuntimeError(
            f"Need {minimum_gib:g} GiB free; available {free / 2**30:.1f} GiB"
        )
    return free
