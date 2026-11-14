"""Read the source CSV in bounded-memory chunks, parsing only the mapped columns."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pandas as pd

from legacy_ingest.errors import ConfigError
from legacy_ingest.schema import SOURCE_COLUMNS


def check_header(path: Path) -> None:
    header = pd.read_csv(path, nrows=0).columns
    missing = [col for col in SOURCE_COLUMNS if col not in header]
    if missing:
        raise ConfigError(f"{path} is missing expected column(s): {', '.join(missing)}")


def read_chunks(path: Path, chunk_rows: int) -> Iterator[pd.DataFrame]:
    """Yield raw chunks as strings; typing happens in ``transform`` so bad cells become
    rejects instead of crashing the parser."""
    check_header(path)
    yield from pd.read_csv(path, usecols=list(SOURCE_COLUMNS), dtype=str, chunksize=chunk_rows)


def file_fingerprint(path: Path, block_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(block_size):
            digest.update(block)
    return digest.hexdigest()
