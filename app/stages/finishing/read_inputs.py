"""Shared deterministic read normalization for HiFi finishing tools."""

from pathlib import Path
from typing import Protocol, Sequence

from app.stages.sequences import read_sequences


class ReadMember(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def format(self) -> str: ...


def write_normalized_fasta(path: Path, members: Sequence[ReadMember]) -> None:
    """Preserve member/record order with collision-free executor-owned names."""
    with path.open("w", encoding="ascii") as handle:
        for index, member in enumerate(members):
            for ordinal, (_, sequence) in enumerate(
                read_sequences(Path(member.path), member.format)
            ):
                handle.write(f">member_{index}_read_{ordinal}\n{sequence}\n")
