"""Invocation-local disk index for bounded-memory paired sequence validation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from types import TracebackType
from typing import Self

from app.stages.sequences import read_sequences


class SequenceIndex(Mapping[str, str]):
    """Validate once, retaining at most one scaffold in memory per index.

    This is disposable scratch data, never a persisted source of evidence. Names
    are unique and iteration preserves the input order; callers may compare files
    whose scaffold orders differ. Closing removes the scratch database.
    """

    def __init__(self, path: Path, sequence_format: str = "fasta") -> None:
        self._scratch = TemporaryDirectory(prefix="gene-sequences-")
        self._db = sqlite3.connect(str(Path(self._scratch.name) / "sequences.sqlite"))
        self._cached: tuple[str, str] | None = None
        try:
            self._db.execute("PRAGMA cache_size=-2048")
            self._db.execute(
                "CREATE TABLE sequences (name TEXT PRIMARY KEY, sequence TEXT NOT NULL)"
            )
            with self._db:
                for name, sequence in read_sequences(path, sequence_format):
                    try:
                        self._db.execute(
                            "INSERT INTO sequences VALUES (?, ?)", (name, sequence)
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ValueError(
                            f"duplicate scaffold identifier: {name}"
                        ) from exc
        except BaseException:
            self.close()
            raise

    def __getitem__(self, name: str) -> str:
        if self._cached is not None and self._cached[0] == name:
            return self._cached[1]
        row = self._db.execute(
            "SELECT sequence FROM sequences WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise KeyError(name)
        self._cached = (name, str(row[0]))
        return self._cached[1]

    def __iter__(self) -> Iterator[str]:
        for row in self._db.execute("SELECT name FROM sequences ORDER BY rowid"):
            yield str(row[0])

    def __len__(self) -> int:
        return int(self._db.execute("SELECT count(*) FROM sequences").fetchone()[0])

    def lengths(self) -> Mapping[str, int]:
        return SequenceLengths(self)

    def close(self) -> None:
        self._cached = None
        self._db.close()
        self._scratch.cleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class SequenceLengths(Mapping[str, int]):
    """Length-only SQL projection without loading sequence bytes."""

    def __init__(self, index: SequenceIndex) -> None:
        self.index = index

    def __getitem__(self, name: str) -> int:
        row = self.index._db.execute(
            "SELECT length(sequence) FROM sequences WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise KeyError(name)
        return int(row[0])

    def __iter__(self) -> Iterator[str]:
        return iter(self.index)

    def __len__(self) -> int:
        return len(self.index)
