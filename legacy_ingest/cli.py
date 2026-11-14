"""Legacy MSSQL loader entrypoint.

    python -m legacy_ingest                       # atomic full refresh (staging -> swap)
    python -m legacy_ingest --mode upsert         # MERGE into the live table on TS_UTC
    python -m legacy_ingest --dry-run             # extract/transform/validate only; no DB access
    python -m legacy_ingest --force               # reload even if the CSV is unchanged

Exit codes: 0 = success / skipped / dry-run ok, 1 = load or validation failure,
2 = configuration error, 3 = another load is running.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from pydantic import ValidationError

from legacy_ingest.config import get_legacy_settings
from legacy_ingest.errors import ConfigError, IngestLocked
from rag.config import PROJECT_ROOT, configure_logging

logger = logging.getLogger("legacy_ingest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the fleet-history CSV into the legacy MSSQL table.")
    parser.add_argument("--mode", choices=("refresh", "upsert"), default="refresh",
                        help="refresh: atomic full replace (default); upsert: MERGE on TS_UTC")
    parser.add_argument("--csv", type=Path, help="Override RAW_DATA_PATH")
    parser.add_argument("--dry-run", action="store_true", help="Validate the CSV without touching the database")
    parser.add_argument("--force", action="store_true", help="Load even if the CSV is unchanged since the last run")
    parser.add_argument("--chunk-rows", type=int, help="Override CSV_CHUNK_ROWS")
    parser.add_argument("--max-reject-ratio", type=float, help="Override MAX_REJECT_RATIO (0-1)")
    parser.add_argument("--log-level", help="Override LEGACY_LOG_LEVEL")
    args = parser.parse_args(argv)

    try:
        settings = get_legacy_settings()
        overrides: dict = {}
        if args.csv:
            overrides["raw_data_path"] = (args.csv if args.csv.is_absolute() else Path.cwd() / args.csv).resolve()
        if args.chunk_rows:
            overrides["csv_chunk_rows"] = args.chunk_rows
        if args.max_reject_ratio is not None:
            overrides["max_reject_ratio"] = args.max_reject_ratio
        if args.log_level:
            overrides["legacy_log_level"] = args.log_level
        # Round-trip through validation so CLI overrides get the same checks as env values.
        settings = type(settings).model_validate({**settings.model_dump(), **overrides})
    except ValidationError as exc:
        configure_logging("INFO")
        logger.error("Invalid configuration:\n%s", exc)
        return 2
    configure_logging(settings.legacy_log_level)

    if not settings.raw_data_path.is_file():
        logger.error(
            "Dataset not found at %s. Download it from the URL in '%s' into 'data/raw/'.",
            settings.raw_data_path, PROJECT_ROOT / "data" / "source" / "data.txt",
        )
        return 2

    from legacy_ingest.loader import Loader, dry_run

    try:
        if args.dry_run:
            report = dry_run(settings)
        else:
            from legacy_ingest.db import connect

            report = Loader(settings, connect).run(mode=args.mode, force=args.force)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2
    except IngestLocked as exc:
        logger.error("%s", exc)
        return 3
    except Exception:  # noqa: BLE001 - e.g. connection lost before the run was audited
        logger.exception("Load aborted.")
        return 1

    logger.info(
        "%s [%s] in %.2fs: %d read, %d loaded, %d rejected%s%s",
        report.status, report.mode, report.duration_s, report.rows_read, report.rows_loaded,
        report.rows_rejected,
        f", {report.rows_inserted} inserted, {report.rows_updated} updated" if report.rows_inserted is not None else "",
        f" | {report.message}" if report.message else "",
    )
    if report.reject_file:
        logger.warning("Rejected rows written to %s", report.reject_file)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
