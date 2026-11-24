"""Options and targets for one generation run."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data" / "synthetic"
SYNTH_TABLE = "TBL_SC_FLEET_SYNTH_RAW"

PROFILES = ("operational", "historical")
SCENARIOS = ("none", "cooling_failure", "port_congestion", "high_risk_delay", "clean")

# The raw dataset ends at 2024-08-29 00:00; synthetic backfill starts one hour later.
BACKFILL_START = datetime(2024, 8, 29, 1, 0)
LIVE_HOURS = 24
LIVE_INTERVAL_S = 900
SCENARIO_HOURS = 3
SCENARIO_TRUCKS = 3

# risk_classification in the raw dataset is disruption_likelihood_score cut at these values (exact).
RISK_CUTS = (0.3, 0.7)

# Accepted share of backfill rows per SOP rule, operational profile. Port congestion is counted over
# readings inside the port area only. Shares are over the whole backfill, so seasons average out.
OPERATIONAL_TARGETS: dict[str, tuple[float, float]] = {
    "TEMP_BREACH": (0.01, 0.07),
    "TEMP_BELOW_RANGE": (0.0, 0.03),
    "PORT_CONGESTION": (0.03, 0.20),
    "HIGH_RISK_DELAY": (0.01, 0.10),
}
MIN_BACKFILL_ROWS_FOR_TARGETS = 24 * 365  # a shorter run doesn't cover every season
MAX_KS = 0.03  # historical profile: largest allowed two-sample KS distance per column


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


@dataclass(frozen=True)
class SynthConfig:
    profile: str = "operational"
    scenario: str = "none"
    seed: int = 7
    end: datetime = datetime(2026, 1, 1)
    trucks: int = 24
    backfill: bool = True
    backfill_start: datetime = BACKFILL_START

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
        if self.scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {', '.join(SCENARIOS)}")
        if self.scenario != "none" and self.profile != "operational":
            raise ValueError("scenarios need the operational profile")
        if not SCENARIO_TRUCKS <= self.trucks <= LIVE_INTERVAL_S:
            raise ValueError(f"trucks must be between {SCENARIO_TRUCKS} and {LIVE_INTERVAL_S}")
        if self.end.tzinfo is not None:
            raise ValueError("end must be a naive UTC time")
        if self.end.microsecond:
            raise ValueError("end must be a whole second")
        if self.backfill and self.live_start <= self.backfill_start:
            raise ValueError(f"end must be more than {LIVE_HOURS} h after {self.backfill_start}")

    @property
    def live_start(self) -> datetime:
        return self.end - timedelta(hours=LIVE_HOURS)
