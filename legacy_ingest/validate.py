"""Row-level data-quality rules. Bad rows are quarantined to CSV, never sent to the database."""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from legacy_ingest.schema import PRIMARY_KEY, RISK_CLASSES, RISK_COLUMN

logger = logging.getLogger(__name__)

REJECT_REASON = "REJECT_REASON"

# Inclusive plausible ranges; NaN (missing or non-numeric in the CSV) fails the check too.
RANGES: dict[str, tuple[float, float]] = {
    "V_LAT": (-90.0, 90.0),
    "V_LON": (-180.0, 180.0),
    "IOT_TEMP_VAL_C": (-60.0, 80.0),
    "CGO_COND_CD": (0.0, 1.0),
    "DELAY_PROB_DEC": (0.0, 1.0),
    "PRT_CNG_LVL": (0.0, 10.0),
    "RT_RSK_IDX": (0.0, 10.0),
}


def _rule_masks(df: pd.DataFrame) -> list[tuple[str, pd.Series]]:
    masks = [(f"{PRIMARY_KEY} missing or not 'YYYY-MM-DD HH:MM:SS'", df[PRIMARY_KEY].isna())]
    for col, (low, high) in RANGES.items():
        masks.append((f"{col} missing or outside [{low:g}, {high:g}]", ~df[col].between(low, high)))
    masks.append((f"{RISK_COLUMN} not in {list(RISK_CLASSES)}", ~df[RISK_COLUMN].isin(RISK_CLASSES)))
    return masks


def split_valid(df: pd.DataFrame, seen_keys: set) -> tuple[pd.DataFrame, pd.Series]:
    """Return ``(valid_rows, reasons)``; ``reasons`` is indexed by the rejected rows.

    ``seen_keys`` carries primary keys accepted from earlier chunks, so duplicates are caught
    across the whole file (first occurrence wins). It is updated in place.
    """
    reasons = pd.Series("", index=df.index, dtype=object)
    for label, mask in _rule_masks(df):
        reasons[mask.to_numpy()] += label + "; "

    # Duplicates are judged only among otherwise-valid rows, so a broken first copy
    # doesn't knock out a good second one.
    candidates = df.loc[reasons == "", PRIMARY_KEY]
    dup = candidates.duplicated(keep="first") | candidates.isin(seen_keys)
    reasons[dup[dup].index] += f"duplicate {PRIMARY_KEY}; "

    ok = reasons == ""
    seen_keys.update(df.loc[ok, PRIMARY_KEY])
    return df[ok], reasons[~ok].str.rstrip("; ")


class RejectSink:
    """Appends rejected raw rows (plus reason) to ``<reject_dir>/<run_id>.csv``, lazily."""

    def __init__(self, reject_dir: Path, run_id: str) -> None:
        self.path = reject_dir / f"{run_id}.csv"
        self.count = 0

    def write(self, raw: pd.DataFrame, reasons: pd.Series) -> None:
        if reasons.empty:
            return
        rejected = raw.loc[reasons.index].assign(**{REJECT_REASON: reasons})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rejected.to_csv(self.path, mode="a", header=self.count == 0, index=False)
        self.count += len(rejected)
        for reason, n in reasons.value_counts().head(5).items():
            logger.warning("Rejected %d row(s): %s", n, reason)
