"""Map a raw CSV chunk onto the legacy column names and SQL-compatible types."""
from __future__ import annotations

import pandas as pd

from legacy_ingest.schema import (
    FLAG_COLUMN,
    LEGACY_MAPPING,
    NUMERIC_COLUMNS,
    PRIMARY_KEY,
    RISK_COLUMN,
    TARGET_COLUMNS,
)

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def to_legacy(raw: pd.DataFrame) -> pd.DataFrame:
    """Pure transform; unparseable values become NaN/NaT and are rejected by ``validate``."""
    df = raw.rename(columns=LEGACY_MAPPING)
    df[PRIMARY_KEY] = pd.to_datetime(df[PRIMARY_KEY], format=TIMESTAMP_FORMAT, errors="coerce")
    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    df[RISK_COLUMN] = df[RISK_COLUMN].str.strip()
    # Fake ingestion flag so the table looks fed by an automated legacy system.
    df[FLAG_COLUMN] = "Y"
    return df[list(TARGET_COLUMNS)]


def to_rows(df: pd.DataFrame) -> list[tuple]:
    """Convert to plain Python tuples in TARGET_COLUMNS order for pyodbc executemany."""
    columns = [
        [ts.to_pydatetime() for ts in df[col]] if col == PRIMARY_KEY else df[col].tolist()
        for col in TARGET_COLUMNS
    ]
    return list(zip(*columns))
