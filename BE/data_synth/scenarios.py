"""Incidents placed in the last few hours of the live window, so a demo question has a known answer.

The scenario trucks are parked at the Port of Long Beach. Other live rows are calmed on the fields a
scenario doesn't concern, so exactly the intended SOP rule triggers (``clean``: none does).
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from agent.intent import GAZETTEER
from data_synth.config import SCENARIO_HOURS, SCENARIO_TRUCKS, SynthConfig
from data_synth.reference import TIMESTAMP

# SOP rules expected to trigger on the live window, per scenario.
EXPECTED: dict[str, set[str]] = {
    "cooling_failure": {"TEMP_BREACH"},
    "port_congestion": {"PORT_CONGESTION"},
    "high_risk_delay": {"HIGH_RISK_DELAY"},
    "clean": set(),
}


def apply_scenario(df: pd.DataFrame, trucks: np.ndarray, cfg: SynthConfig, rng: np.random.Generator) -> None:
    live = (df[TIMESTAMP] >= cfg.live_start).to_numpy()
    recent = (df[TIMESTAMP] > cfg.end - timedelta(hours=SCENARIO_HOURS)).to_numpy()
    actors = recent & (trucks < SCENARIO_TRUCKS)

    _set(df, live, "iot_temperature", lambda v: np.clip(v, 0.4, 3.6))
    _set(df, live, "port_congestion_level", lambda v: np.clip(v, 0.0, 6.4))
    _set(df, live, "delay_probability", lambda v: np.clip(v, 0.0, 0.58))
    n = int(actors.sum())
    if cfg.scenario != "clean":
        lat, lon = GAZETTEER["port of long beach"]
        df.loc[actors, "vehicle_gps_latitude"] = lat + rng.normal(0, 0.006, n)
        df.loc[actors, "vehicle_gps_longitude"] = lon + rng.normal(0, 0.006, n)
    if cfg.scenario == "cooling_failure":
        since = (df.loc[actors, TIMESTAMP] - (cfg.end - timedelta(hours=SCENARIO_HOURS))).dt.total_seconds() / 3600
        df.loc[actors, "iot_temperature"] = 2.2 + 2.6 * since.to_numpy() + rng.normal(0, 0.2, n)
    elif cfg.scenario == "port_congestion":
        _set(df, recent, "port_congestion_level", lambda v: rng.uniform(7.6, 9.2, len(v)))
    elif cfg.scenario == "high_risk_delay":
        df.loc[actors, "disruption_likelihood_score"] = rng.uniform(0.78, 0.95, n)
        df.loc[actors, "delay_probability"] = rng.uniform(0.70, 0.90, n)


def _set(df: pd.DataFrame, mask: np.ndarray, column: str, change) -> None:
    df.loc[mask, column] = change(df.loc[mask, column].to_numpy())
