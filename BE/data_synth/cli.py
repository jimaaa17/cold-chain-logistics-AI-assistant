"""Synthetic telemetry generator.

    python -m data_synth                                  # operational profile, backfill + live 24 h
    python -m data_synth --scenario cooling_failure       # with a known incident near Long Beach
    python -m data_synth --profile historical             # same distributions as the raw dataset
    python -m data_synth --load                           # generate, check, load into the synthetic table
    python -m data_synth --reset                          # delete all synthetic rows

Exit codes: 0 = ok, 1 = checks or load failed, 2 = configuration error.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from data_synth.config import OUTPUT_DIR, PROFILES, SCENARIOS, SYNTH_TABLE, SynthConfig, utc_now_naive

logger = logging.getLogger("data_synth")


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO time: {value!r}") from None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m data_synth", description="Generate synthetic fleet telemetry (never real data).")
    p.add_argument("--profile", choices=PROFILES, default="operational")
    p.add_argument("--scenario", choices=SCENARIOS, default="none")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--end", type=_utc, help="Newest reading, ISO, UTC (default: now)")
    p.add_argument("--trucks", type=int, default=24)
    p.add_argument("--live-only", action="store_true", help="Skip the hourly backfill")
    p.add_argument("--out", type=Path, help="CSV path (default: data/synthetic/<profile>_<scenario>.csv)")
    p.add_argument("--load", action="store_true", help=f"Load into {SYNTH_TABLE} after the checks pass")
    p.add_argument("--reset", action="store_true", help=f"Delete every row of {SYNTH_TABLE} and stop")
    p.add_argument("--allow-remote", action="store_true", help="Permit a SQL host other than localhost")
    p.add_argument("--refresh-reference", action="store_true", help="Rebuild the quantile file from the raw CSV and stop")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s [%(name)s] %(message)s")
    try:
        if args.refresh_reference:
            return _refresh_reference()
        if args.reset:
            from data_synth.load import clear

            logger.info("Removed %d synthetic rows from %s", clear(allow_remote=args.allow_remote), SYNTH_TABLE)
            return 0
        cfg = SynthConfig(
            profile=args.profile, scenario=args.scenario, seed=args.seed, end=args.end or utc_now_naive(),
            trucks=args.trucks, backfill=not args.live_only,
        )
        return _generate(cfg, args)
    except (ValueError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        return 2
    except Exception as exc:
        from data_synth.load import UnsafeTarget
        from legacy_ingest.errors import ConfigError, IngestLocked

        if isinstance(exc, (UnsafeTarget, ConfigError)):
            logger.error("%s", exc)
            return 2
        if isinstance(exc, IngestLocked):
            logger.error("%s", exc)
            return 1
        logger.error("Failed: %s", type(exc).__name__)  # driver messages can quote connection details
        return 1


def _refresh_reference() -> int:
    from data_synth.reference import REFERENCE_PATH, build_reference
    from legacy_ingest.config import DEFAULT_DATA_FILE

    build_reference(DEFAULT_DATA_FILE)
    logger.info("Wrote %s", REFERENCE_PATH)
    return 0


def _generate(cfg: SynthConfig, args: argparse.Namespace) -> int:
    from data_synth.profiles import generate
    from data_synth.reference import Reference
    from data_synth.report import build_report, format_report

    ref = Reference.load()
    df = generate(cfg, ref)
    rep = build_report(df, cfg, ref)
    print(format_report(rep))
    path = (args.out or OUTPUT_DIR / f"{cfg.profile}_{cfg.scenario}.csv").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, float_format="%.15g", date_format="%Y-%m-%d %H:%M:%S")
    manifest = {
        "synthetic": True, "profile": cfg.profile, "scenario": cfg.scenario, "seed": cfg.seed,
        "end_utc": cfg.end.isoformat(), "live_start_utc": cfg.live_start.isoformat(), "trucks": cfg.trucks,
        "rows": rep.rows, "backfill_rows": rep.backfill_rows, "live_rows": rep.live_rows,
        "backfill_rates": rep.backfill_rates, "live_breaches": rep.live_breaches, "problems": rep.problems,
    }
    path.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %s", path)
    if not rep.ok:
        logger.error("Checks failed; not loading.")
        return 1
    if args.load:
        from data_synth.load import load_csv

        result = load_csv(path, allow_remote=args.allow_remote)
        logger.info("%s: %d rows loaded into %s", result.status, result.rows_loaded, SYNTH_TABLE)
        return 0 if result.ok else 1
    return 0
