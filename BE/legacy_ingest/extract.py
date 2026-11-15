"""Read the source CSV in bounded-memory chunks, parsing only the mapped columns."""
from __future__ import annotations

import csv
import hashlib
import io
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pandas as pd

from legacy_ingest.errors import ConfigError
from legacy_ingest.schema import SOURCE_COLUMNS

_SPOOL_MAX_BYTES = 64 * (1 << 20)  # keep snapshots up to 64 MiB in RAM, spill larger to disk


def _validate_header_line(line: str, path: Path) -> None:
    header = next(csv.reader([line]), [])
    missing = [col for col in SOURCE_COLUMNS if col not in header]
    if missing:
        raise ConfigError(f"{path} is missing expected column(s): {', '.join(missing)}")


def check_header(path: Path) -> None:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        _validate_header_line(fh.readline(), path)


class CsvChunkStream(Iterator[pd.DataFrame]):
    """Single-pass snapshot + SHA-256 + lazy single-parse chunk iterator."""

    def __init__(self, path: Path, chunk_rows: int, block_size: int = 1 << 20) -> None:
        self.path = path
        self.chunk_rows = chunk_rows
        self._spool: tempfile.SpooledTemporaryFile[bytes] = tempfile.SpooledTemporaryFile(
            max_size=_SPOOL_MAX_BYTES, mode="w+b"
        )
        try:
            digest = hashlib.sha256()
            with path.open("rb") as fh:
                while block := fh.read(block_size):
                    digest.update(block)
                    self._spool.write(block)
            self.sha256 = digest.hexdigest()
            self._spool.seek(0)
            first_line = self._spool.readline().decode("utf-8", errors="replace")
            _validate_header_line(first_line, path)
            self._spool.seek(0)
        except BaseException:
            self._spool.close()
            raise
        self._iter: Iterator[pd.DataFrame] | None = None

    def __iter__(self) -> "CsvChunkStream":
        return self

    def __next__(self) -> pd.DataFrame:
        if self._iter is None:
            self._iter = self._stream()
        return next(self._iter)

    def _stream(self) -> Iterator[pd.DataFrame]:
        try:
            text_stream = io.TextIOWrapper(self._spool, encoding="utf-8", newline="")
            yield from pd.read_csv(
                text_stream, usecols=list(SOURCE_COLUMNS), dtype=str, chunksize=self.chunk_rows
            )
        finally:
            self.close()

    def close(self) -> None:
        self._spool.close()


def read_chunks(path: Path, chunk_rows: int) -> Iterator[pd.DataFrame]:
    """Yield raw chunks as strings; typing happens in ``transform`` so bad cells become
    rejects instead of crashing the parser."""
    return CsvChunkStream(path, chunk_rows)


def file_fingerprint(path: Path, block_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(block_size):
            digest.update(block)
    return digest.hexdigest()
