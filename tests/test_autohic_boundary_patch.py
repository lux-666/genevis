"""Opt-in regressions against an installed, patched AutoHiC checkout."""

import json
import os
import subprocess

import pytest

# Run under AutoHiC's interpreter: the orchestrator does not own its ML stack.
_CHECK = r"""import hashlib
import json
import sys
from pathlib import Path

root, work = map(Path, sys.argv[1:])
sys.path.insert(0, str(root))
from src.utils import get_chr_data
from src.utils.logger import logger
logger.disabled = True

cases = json.loads(sys.stdin.read())
results = []
for case in cases:
    path = work / case["name"]
    path.mkdir()
    source = path / "source.assembly"
    source.write_text(case["assembly"])
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    ends = path / "chr.txt"
    ends.write_text("Chr\tStart\tEnd\n" + "".join(
        f"{i + 1}\t0\t{end}\n" for i, end in enumerate(case["ends"])))
    output = path / "result.assembly"
    get_chr_data.get_ratio = lambda *_: case.get("ratio", 1)
    get_chr_data.divide_chr(str(ends), "unused.hic", str(source),
                            str(output), case["ends"][-1])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    headers, groups = {}, []
    for line in output.read_text().splitlines():
        fields = line.split()
        if line.startswith(">"):
            headers[int(fields[1])] = (fields[0], int(fields[2]))
        elif fields:
            groups.append([int(value) for value in fields])
    ids = [abs(value) for group in groups for value in group]
    assert len(ids) == len(set(ids)) and set(ids) == set(headers)
    assert all(length > 0 for _, length in headers.values())
    assert sum(length for _, length in headers.values()) == case["total"]
    group_lengths = [sum(headers[abs(value)][1] for value in group) for group in groups]
    assert group_lengths == case["groups"], (case["name"], group_lengths)
    if "orders" in case:
        assert groups == case["orders"], (case["name"], groups)
    if "fragment_lengths" in case:
        assert [length for _, length in headers.values()] == case["fragment_lengths"]
    results.append(dict(name=case["name"], group_lengths=group_lengths,
                        fragments=len(headers)))
print(json.dumps(results))
"""


@pytest.mark.real_tool
def test_installed_autohic_boundary_cutting(tmp_path):
    root = os.environ.get("AUTOHIC_TEST_ROOT")
    python = os.environ.get("AUTOHIC_TEST_PYTHON")
    if not root or not python:
        pytest.skip("Set AUTOHIC_TEST_ROOT and AUTOHIC_TEST_PYTHON explicitly")
    cases = [
        dict(
            name="reverse_end",
            assembly=">a 1 10\n>b 2 8\n-1\n2\n",
            ends=[10],
            total=18,
            groups=[10, 8],
            orders=[[-1], [2]],
        ),
        dict(
            name="forward_end",
            assembly=">a 1 10\n>b 2 8\n1\n2\n",
            ends=[10],
            total=18,
            groups=[10, 8],
            orders=[[1], [2]],
        ),
        dict(
            name="existing_join_and_eof",
            assembly=">a 1 10\n>b 2 8\n1 -2\n",
            ends=[10, 18],
            total=18,
            groups=[10, 8],
            orders=[[1], [-2]],
        ),
        dict(
            name="forward_internal",
            assembly=">a 1 10\n>b 2 8\n1 2\n",
            ends=[4, 18],
            total=18,
            groups=[4, 14],
            orders=[[1], [2, 3]],
            fragment_lengths=[4, 6, 8],
        ),
        dict(
            name="reverse_internal",
            assembly=">a 1 10\n>b 2 8\n-1 2\n",
            ends=[4, 18],
            total=18,
            groups=[4, 14],
            orders=[[-2], [-1, 3]],
            fragment_lengths=[6, 4, 8],
        ),
        dict(
            name="repeated_reverse_cuts",
            assembly=">a 1 10\n>b 2 8\n-1 2\n",
            ends=[2, 7, 10],
            total=18,
            groups=[2, 5, 3, 8],
            orders=[[-3], [-2], [-1], [4]],
            fragment_lengths=[3, 5, 2, 8],
        ),
        dict(
            name="scaled_map",
            assembly=">a 1 10\n>b 2 8\n1 -2\n",
            ends=[3, 9],
            ratio=2,
            total=18,
            groups=[6, 12],
            orders=[[1], [2, -3]],
            fragment_lengths=[6, 4, 8],
        ),
    ]
    process = subprocess.run(
        [python, "-c", _CHECK, root, str(tmp_path)],
        input=json.dumps(cases),
        text=True,
        capture_output=True,
        timeout=180,
        env={
            **os.environ,
            "MPLBACKEND": "Agg",
            "OMP_NUM_THREADS": "4",
            "MKL_NUM_THREADS": "4",
        },
    )
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout.splitlines()[-1])
    assert len(result) == len(cases)
