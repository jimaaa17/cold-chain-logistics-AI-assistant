"""Row generators. Both return the raw dataset's columns; the same config and seed give the same rows.

``historical`` samples each column on its own from the raw dataset's distribution. That matches the raw
data, whose rows and columns are statistically independent (no routes, no daily or seasonal cycle).

``operational`` models a small Southern California fleet instead: trucks on fixed lanes, a setpoint-
controlled cargo temperature with occasional excursions, and port congestion with daily, weekly and
peak-season cycles that raise the delay probability. These cycles are assumptions, not measurements.
Columns no SOP rule or view field uses keep their historical distribution.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from agent.intent import GAZETTEER
from data_synth.config import BACKFILL_START, LIVE_INTERVAL_S, RISK_CUTS, SynthConfig
from data_synth.reference import RISK, TIMESTAMP, Reference

LANES = (
    ("port of long beach", "inland empire"),
    ("port of los angeles", "san bernardino"),
    ("port of long beach", "riverside"),
    ("san pedro", "los angeles"),
    ("port of los angeles", "san diego"),
    ("long beach", "san bernardino"),
)
PACIFIC_OFFSET_H = -8  # daily cycles follow local time; daylight saving is ignored


def risk_class(score: np.ndarray) -> np.ndarray:
    low, high = RISK_CUTS
    return np.where(score >= high, "High Risk", np.where(score >= low, "Moderate Risk", "Low Risk"))


def backfill_times(cfg: SynthConfig) -> list[datetime]:
    """One reading per hour, like the raw dataset, up to (not including) the live window."""
    if not cfg.backfill:
        return []
    hours = int((cfg.live_start - cfg.backfill_start).total_seconds() // 3600)
    times = [cfg.backfill_start + timedelta(hours=h) for h in range(hours + 1)]
    return [t for t in times if t < cfg.live_start]


def live_times(cfg: SynthConfig) -> list[tuple[datetime, int]]:
    """(time, truck) for the live window, oldest first. Seconds are staggered per truck because
    TS_UTC is the table's primary key. Truck 0 reports at exactly `end`."""
    step = LIVE_INTERVAL_S // cfg.trucks
    slots = 24 * 3600 // LIVE_INTERVAL_S
    out = [
        (cfg.end - timedelta(seconds=slot * LIVE_INTERVAL_S + truck * step), truck)
        for slot in range(slots) for truck in range(cfg.trucks)
    ]
    return sorted(out)


def generate(cfg: SynthConfig, ref: Reference) -> pd.DataFrame:
    rng = np.random.default_rng(cfg.seed)
    history = backfill_times(cfg)
    live = live_times(cfg)
    times = history + [t for t, _ in live]
    trucks = np.array([i % cfg.trucks for i in range(len(history))] + [k for _, k in live])
    df = _independent(ref, len(times), rng)
    df[TIMESTAMP] = pd.to_datetime(pd.Series(times))
    if cfg.profile == "operational":
        _operational(df, trucks, cfg, rng)
    df[RISK] = risk_class(df["disruption_likelihood_score"].to_numpy())
    if cfg.scenario != "none":
        from data_synth.scenarios import apply_scenario

        apply_scenario(df, trucks, cfg, rng)
        df[RISK] = risk_class(df["disruption_likelihood_score"].to_numpy())
    return df[ref.columns]


def _independent(ref: Reference, n: int, rng: np.random.Generator) -> pd.DataFrame:
    return pd.DataFrame({c: ref.sample(c, rng.random(n)) for c in ref.columns if c not in (TIMESTAMP, RISK)})


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _ar1(n: int, phi: float, sd: float, rng: np.random.Generator) -> np.ndarray:
    """Stationary AR(1) noise with standard deviation `sd`, one value per hour."""
    noise = rng.normal(0.0, sd * np.sqrt(1 - phi**2), n)
    out = np.empty(n)
    level = rng.normal(0.0, sd)
    for i in range(n):
        level = phi * level + noise[i]
        out[i] = level
    return out


def _season(day_of_year: np.ndarray, peak_day: float) -> np.ndarray:
    return np.cos(2 * np.pi * (day_of_year - peak_day) / 365.25)


def _daily(hour: np.ndarray, peak_hour: float) -> np.ndarray:
    return np.cos(2 * np.pi * (hour - peak_hour) / 24.0)


def _operational(df: pd.DataFrame, trucks: np.ndarray, cfg: SynthConfig, rng: np.random.Generator) -> None:
    n = len(df)
    local = df[TIMESTAMP] + pd.Timedelta(hours=PACIFIC_OFFSET_H)
    hour = (local.dt.hour + local.dt.minute / 60).to_numpy(dtype=float)
    doy = local.dt.dayofyear.to_numpy(dtype=float)
    weekday = local.dt.dayofweek.to_numpy()
    # Hours since the start, for processes that evolve in time (float; live rows fall between hours).
    start = df[TIMESTAMP].min()
    t = ((df[TIMESTAMP] - start).dt.total_seconds() / 3600).to_numpy()
    grid = np.arange(int(np.ceil(t.max())) + 2)

    ambient = 18 + 5 * _season(doy, 225) + 5 * _daily(hour, 14) + np.interp(t, grid, _ar1(len(grid), 0.95, 1.5, rng))
    congestion = (
        3.6 + 1.3 * _season(doy, 265) + 0.8 * _daily(hour, 13) + np.where(weekday < 5, 0.5, -0.9)
        + np.interp(t, grid, _ar1(len(grid), 0.96, 1.3, rng))
    )
    traffic = 4.0 + 2.2 * _daily(hour, 16) + 1.0 * _daily(hour * 2, 16) + np.where(weekday < 5, 0.8, -1.2)
    traffic = np.clip(traffic + rng.normal(0, 0.9, n), 0, 10)

    _positions(df, trucks, t, rng)
    temp, excursion = _temperatures(ambient, trucks, t, cfg, rng)
    df["iot_temperature"] = temp
    df["port_congestion_level"] = np.clip(congestion + rng.normal(0, 0.25, n), 0, 10)
    df["traffic_congestion_level"] = traffic
    df["weather_condition_severity"] = np.clip(0.15 + 0.1 * _season(doy, 20) + rng.gamma(1.2, 0.08, n), 0, 1)
    pressure = 0.55 * (df["port_congestion_level"].to_numpy() - 4) + 0.3 * (traffic - 4)
    df["delay_probability"] = _sigmoid(-1.6 + pressure + rng.normal(0, 0.7, n))
    df["disruption_likelihood_score"] = _sigmoid(-1.5 + 0.8 * pressure + 2.5 * excursion + rng.normal(0, 0.7, n))
    df["route_risk_level"] = np.clip(10 * _sigmoid(-1.2 + 0.5 * pressure + rng.normal(0, 0.5, n)), 0, 10)
    df["eta_variation_hours"] = np.clip(-0.4 + 4.0 * df["delay_probability"] + rng.normal(0, 0.4, n), -2, 5)


def _positions(df: pd.DataFrame, trucks: np.ndarray, t: np.ndarray, rng: np.random.Generator) -> None:
    """Each truck shuttles along its lane (about 2 h each way) and waits about 1 h at both ends."""
    lane = trucks % len(LANES)
    ends = np.array([[GAZETTEER[a], GAZETTEER[b]] for a, b in LANES])  # lane, end, (lat, lon)
    phase = ((t + trucks * 0.73) % 6.0)  # 0-1 wait, 1-3 out, 3-4 wait, 4-6 back
    share = np.select([phase < 1, phase < 3, phase < 4], [0.0, (phase - 1) / 2, 1.0], (6.0 - phase) / 2)
    point = ends[lane, 0] + share[:, None] * (ends[lane, 1] - ends[lane, 0])
    point = point + rng.normal(0, 0.006, point.shape)
    df["vehicle_gps_latitude"], df["vehicle_gps_longitude"] = point[:, 0], point[:, 1]


def _temperatures(
    ambient: np.ndarray, trucks: np.ndarray, t: np.ndarray, cfg: SynthConfig, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """Setpoint control near 2 °C plus excursions: a ramp up (unit failure) or down (overcooling), then
    recovery. Failures are more likely in hot weather. Returns (temperature, excursion strength 0-1)."""
    setpoint = 2.0 + 0.025 * (ambient - 18)
    temp = setpoint + rng.normal(0, 0.45, len(t))
    strength = np.zeros(len(t))
    hours = float(t.max() - t.min())
    for truck in range(cfg.trucks):
        mine = np.flatnonzero(trucks == truck)
        if mine.size == 0:
            continue
        for kind, per_hour in (("hot", 0.016), ("cold", 0.003)):
            for begin in rng.uniform(t.min(), t.max(), rng.poisson(per_hour * hours)):
                heat = float(np.interp(begin, t[mine], ambient[mine]))
                if rng.random() > np.clip(0.4 + 0.05 * (heat - 18), 0.1, 1.0):
                    continue
                length = rng.uniform(2.0, 6.0)
                inside = mine[(t[mine] >= begin) & (t[mine] <= begin + length)]
                shape = np.sin(np.pi * (t[inside] - begin) / length)
                peak = rng.uniform(4.0, 11.0) if kind == "hot" else rng.uniform(-5.0, -2.5)
                temp[inside] += peak * shape
                if kind == "hot":
                    strength[inside] = np.maximum(strength[inside], shape)
    return np.clip(temp, -10, 25), strength
