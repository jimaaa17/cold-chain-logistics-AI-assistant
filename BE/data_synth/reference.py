"""Empirical quantiles of the raw dataset, so generation and tests don't need the CSV itself.

Rebuild with ``python -m data_synth --refresh-reference`` after the raw dataset changes.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

REFERENCE_PATH = Path(__file__).with_name("reference_quantiles.json")
POINTS = 401
TIMESTAMP, RISK = "timestamp", "risk_classification"


class Reference:
    def __init__(self, columns: list[str], quantiles: dict[str, list[float]]) -> None:
        self.columns = columns  # CSV column order, including timestamp and risk_classification
        self.quantiles = {name: np.asarray(values, dtype=float) for name, values in quantiles.items()}

    @classmethod
    def load(cls, path: Path = REFERENCE_PATH) -> "Reference":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["columns"], data["quantiles"])

    def sample(self, column: str, u: np.ndarray) -> np.ndarray:
        """Inverse-CDF sampling: `u` uniform in [0, 1] gives the column's historical distribution."""
        q = self.quantiles[column]
        return np.interp(u, np.linspace(0.0, 1.0, len(q)), q)


def build_reference(csv_path: Path, path: Path = REFERENCE_PATH) -> Reference:
    import pandas as pd

    df = pd.read_csv(csv_path)
    grid = np.linspace(0.0, 1.0, POINTS)
    quantiles = {
        c: [float(f"{v:.12g}") for v in np.quantile(df[c].to_numpy(dtype=float), grid)]
        for c in df.columns if c not in (TIMESTAMP, RISK)
    }
    body = {"rows": len(df), "columns": list(df.columns), "quantiles": quantiles}
    path.write_text(json.dumps(body, separators=(",", ":")) + "\n", encoding="utf-8")
    return Reference(body["columns"], quantiles)


def raw_columns(csv_path: Path, columns: list[str]) -> dict[str, np.ndarray] | None:
    """The raw dataset's columns, or None when the CSV (git-ignored) isn't on this machine."""
    if not csv_path.is_file():
        return None
    import pandas as pd

    df = pd.read_csv(csv_path, usecols=columns)
    return {c: df[c].to_numpy(dtype=float) for c in columns}


def ks_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Two-sample Kolmogorov-Smirnov distance (0 = same distribution, 1 = disjoint)."""
    a, b = np.sort(a), np.sort(b)
    grid = np.concatenate([a, b])
    return float(np.max(np.abs(
        np.searchsorted(a, grid, side="right") / len(a) - np.searchsorted(b, grid, side="right") / len(b)
    )))
