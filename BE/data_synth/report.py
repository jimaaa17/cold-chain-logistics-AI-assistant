"""Checks on generated rows. The SOP rates come from the agent's own rules, not a second copy."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from agent.rules import PORTS_LB_LA_BOX, RULES
from data_synth.config import MAX_KS, MIN_BACKFILL_ROWS_FOR_TARGETS, OPERATIONAL_TARGETS, RISK_CUTS, SynthConfig
from data_synth.profiles import risk_class
from data_synth.reference import RISK, TIMESTAMP, Reference, ks_distance, raw_columns
from data_synth.scenarios import EXPECTED
from legacy_ingest.config import DEFAULT_DATA_FILE
from legacy_ingest.schema import LEGACY_MAPPING, RISK_CLASSES, SOURCE_COLUMNS
from legacy_ingest.validate import RANGES

_VIEW_FIELDS = {  # raw column -> the name rules read in the evidence ledger
    "vehicle_gps_latitude": "Latitude",
    "vehicle_gps_longitude": "Longitude",
    "iot_temperature": "Current_Temperature_C",
    "risk_classification": "Risk_Classification",
    "delay_probability": "Delay_Probability",
    "port_congestion_level": "Port_Congestion_Level",
}


@dataclass
class Report:
    rows: int = 0
    backfill_rows: int = 0
    live_rows: int = 0
    backfill_rates: dict[str, float] = field(default_factory=dict)
    live_breaches: dict[str, int] = field(default_factory=dict)
    ks: dict[str, float] = field(default_factory=dict)
    ks_source: str = ""  # what the KS distances were measured against
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def breaches(df: pd.DataFrame) -> dict[str, np.ndarray]:
    records = df[list(_VIEW_FIELDS)].rename(columns=_VIEW_FIELDS).to_dict("records")
    return {rule.id: np.array([rule.check(r) is not None for r in records], dtype=bool) for rule in RULES}


def _in_port_area(df: pd.DataFrame) -> np.ndarray:
    lat_min, lat_max, lon_min, lon_max = PORTS_LB_LA_BOX
    return (df["vehicle_gps_latitude"].between(lat_min, lat_max)
            & df["vehicle_gps_longitude"].between(lon_min, lon_max)).to_numpy()


def build_report(df: pd.DataFrame, cfg: SynthConfig, ref: Reference) -> Report:
    live = (df[TIMESTAMP] >= cfg.live_start).to_numpy()
    rep = Report(rows=len(df), backfill_rows=int((~live).sum()), live_rows=int(live.sum()))
    _structure(df, cfg, ref, rep)
    found = breaches(df)
    port = _in_port_area(df)
    if rep.backfill_rows:
        for rule, hit in found.items():
            base = ~live & port if rule == "PORT_CONGESTION" else ~live
            rep.backfill_rates[rule] = float(hit[base].mean()) if base.any() else 0.0
    rep.live_breaches = {rule: int(hit[live].sum()) for rule, hit in found.items()}
    if cfg.profile == "operational" and rep.backfill_rows >= MIN_BACKFILL_ROWS_FOR_TARGETS:
        for rule, (low, high) in OPERATIONAL_TARGETS.items():
            rate = rep.backfill_rates[rule]
            if not low <= rate <= high:
                rep.problems.append(f"{rule} rate {rate:.1%} is outside the target {low:.0%}-{high:.0%}")
    if cfg.scenario in EXPECTED:
        triggered = {rule for rule, count in rep.live_breaches.items() if count}
        if triggered != EXPECTED[cfg.scenario]:
            rep.problems.append(
                f"scenario {cfg.scenario}: expected {sorted(EXPECTED[cfg.scenario])}, live rows breach {sorted(triggered)}"
            )
    if cfg.profile == "historical":
        _distributions(df, ref, rep)
    return rep


def _distributions(df: pd.DataFrame, ref: Reference, rep: Report) -> None:
    raw = raw_columns(DEFAULT_DATA_FILE, list(ref.quantiles))
    rep.ks_source = "raw dataset" if raw else "stored quantiles (raw CSV not found)"
    grid = np.linspace(0, 1, 20_000)
    for column in ref.quantiles:
        target = raw[column] if raw else ref.sample(column, grid)
        rep.ks[column] = ks_distance(df[column].to_numpy(), target)
        if rep.ks[column] > MAX_KS:
            rep.problems.append(f"{column}: KS distance {rep.ks[column]:.3f} exceeds {MAX_KS}")


def _structure(df: pd.DataFrame, cfg: SynthConfig, ref: Reference, rep: Report) -> None:
    if list(df.columns) != ref.columns or set(SOURCE_COLUMNS) - set(df.columns):
        rep.problems.append("columns differ from the raw dataset")
        return
    if df.isna().any().any():
        rep.problems.append("missing values")
    if df[TIMESTAMP].duplicated().any():
        rep.problems.append("duplicate timestamps (TS_UTC is the primary key)")
    if df[TIMESTAMP].max() > cfg.end:
        rep.problems.append("timestamps after the end time")
    for source, target in LEGACY_MAPPING.items():
        if target in RANGES and not df[source].between(*RANGES[target]).all():
            rep.problems.append(f"{source} outside the loader's range {RANGES[target]}")
    expected = risk_class(df["disruption_likelihood_score"].to_numpy())
    if not set(df[RISK]) <= set(RISK_CLASSES) or (df[RISK].to_numpy() != expected).any():
        rep.problems.append(f"risk_classification does not follow the score cuts {RISK_CUTS}")


def format_report(rep: Report) -> str:
    lines = [f"rows: {rep.rows} ({rep.backfill_rows} backfill, {rep.live_rows} live)"]
    for rule, rate in rep.backfill_rates.items():
        note = " of readings in the port area" if rule == "PORT_CONGESTION" else ""
        lines.append(f"backfill {rule}: {rate:.1%}{note}")
    for rule, count in rep.live_breaches.items():
        lines.append(f"live {rule}: {count} readings")
    if rep.ks:
        worst = max(rep.ks, key=rep.ks.get)
        lines.append(f"largest KS distance to the {rep.ks_source}: {rep.ks[worst]:.3f} ({worst})")
    lines += [f"PROBLEM: {p}" for p in rep.problems] or ["all checks passed"]
    return "\n".join(lines)
