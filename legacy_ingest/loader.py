"""Orchestrates one load: lock -> staging bulk load -> reconcile -> atomic publish -> audit.

Modes
-----
``refresh`` (default): load ``<table>__STG``, then in ONE transaction rename live -> ``__OLD``,
staging -> live, drop ``__OLD``. Readers see either the old table or the new one, never a
partial load, and any failure leaves the live table untouched.

``upsert``: load staging, then ``MERGE`` into live on ``TS_UTC`` (insert new, update changed).
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import pandas as pd

from legacy_ingest import db
from legacy_ingest.audit import AuditLog
from legacy_ingest.config import LegacySettings
from legacy_ingest.errors import ReconciliationError, ValidationFailed
from legacy_ingest.extract import file_fingerprint, read_chunks
from legacy_ingest.schema import (
    LOADER_VERSION,
    OLD_SUFFIX,
    PRIMARY_KEY,
    STAGING_SUFFIX,
    TableRef,
    constraint_names,
    create_table_sql,
    insert_sql,
    merge_sql,
)
from legacy_ingest.transform import to_legacy, to_rows
from legacy_ingest.validate import RejectSink, split_valid

logger = logging.getLogger(__name__)

Mode = Literal["refresh", "upsert"]
Status = Literal["SUCCEEDED", "SKIPPED", "FAILED", "DRY_RUN"]


@dataclass
class LoadReport:
    run_id: str
    mode: str
    status: Status = "FAILED"
    rows_read: int = 0
    rows_loaded: int = 0
    rows_rejected: int = 0
    rows_inserted: int | None = None
    rows_updated: int | None = None
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    reject_file: str | None = None
    message: str = ""
    duration_s: float = 0.0
    _started: float = field(default_factory=time.perf_counter, repr=False)

    @property
    def ok(self) -> bool:
        return self.status != "FAILED"

    def finish(self, status: Status, message: str = "") -> "LoadReport":
        self.status, self.message = status, message
        self.duration_s = time.perf_counter() - self._started
        return self


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # DATETIME2 has no offset


def new_run_id() -> str:
    return str(uuid.uuid4())


def prepare(settings: LegacySettings, report: LoadReport, sink: RejectSink) -> Iterator[pd.DataFrame]:
    """Extract -> transform -> validate, yielding valid chunks and tallying ``report``."""
    seen_keys: set = set()
    for raw in read_chunks(settings.raw_data_path, settings.csv_chunk_rows):
        df = to_legacy(raw)
        valid, reasons = split_valid(df, seen_keys)
        sink.write(raw, reasons)
        report.rows_read += len(raw)
        report.rows_rejected += len(reasons)
        report.rows_loaded += len(valid)
        if not valid.empty:
            lo, hi = valid[PRIMARY_KEY].min().to_pydatetime(), valid[PRIMARY_KEY].max().to_pydatetime()
            report.ts_min = lo if report.ts_min is None else min(report.ts_min, lo)
            report.ts_max = hi if report.ts_max is None else max(report.ts_max, hi)
        logger.info("Chunk: %d read, %d valid (total %d/%d)", len(raw), len(valid),
                    report.rows_loaded, report.rows_read)
        yield valid
    if sink.count:
        report.reject_file = str(sink.path)


def check_quality(report: LoadReport, max_reject_ratio: float) -> None:
    if report.rows_loaded == 0:
        raise ValidationFailed(f"No valid rows out of {report.rows_read}; refusing to publish.")
    ratio = report.rows_rejected / report.rows_read
    if ratio > max_reject_ratio:
        raise ValidationFailed(
            f"{report.rows_rejected}/{report.rows_read} rows rejected ({ratio:.2%}) exceeds "
            f"MAX_REJECT_RATIO={max_reject_ratio:.2%}; see {report.reject_file}"
        )


def dry_run(settings: LegacySettings) -> LoadReport:
    """Full extract/transform/validate pass with no database access."""
    report = LoadReport(run_id=new_run_id(), mode="dry-run")
    for _ in prepare(settings, report, RejectSink(settings.reject_dir, report.run_id)):
        pass
    try:
        check_quality(report, settings.max_reject_ratio)
    except ValidationFailed as exc:
        return report.finish("FAILED", str(exc))
    return report.finish("DRY_RUN")


class Loader:
    def __init__(self, settings: LegacySettings, connect: Callable[[LegacySettings], object] = db.connect):
        self.settings = settings
        self._connect = connect
        self.live = TableRef(settings.target_schema, settings.target_table)
        self.staging = self.live.with_suffix(STAGING_SUFFIX)
        self.old = self.live.with_suffix(OLD_SUFFIX)
        self.audit = AuditLog(settings.target_schema)

    def run(self, mode: Mode = "refresh", force: bool = False) -> LoadReport:
        """Raises ConfigError / IngestLocked before any work; other failures return a FAILED report."""
        report = LoadReport(run_id=new_run_id(), mode=mode)
        source = self.settings.raw_data_path
        sha = file_fingerprint(source)
        conn = self._connect(self.settings)
        try:
            with db.app_lock(conn, f"legacy_ingest:{self.live.object_name}"):
                cursor = conn.cursor()
                self.audit.ensure(cursor)
                conn.commit()

                if not force and self._unchanged(cursor, mode, sha):
                    report.finish("SKIPPED", f"{source.name} unchanged since last successful {mode}")
                    self._audit_start(cursor, report, source, sha)
                    self._audit_finish(cursor, report)
                    conn.commit()
                    return report

                self._audit_start(cursor, report, source, sha)
                conn.commit()
                try:
                    self._load_staging(conn, cursor, report)
                    if mode == "refresh":
                        self._publish_swap(cursor)
                    else:
                        self._publish_merge(cursor, report)
                    conn.commit()
                    report.finish("SUCCEEDED")
                except Exception as exc:  # noqa: BLE001 - recorded, then reported via exit code
                    logger.exception("Load failed; live table left unchanged.")
                    conn.rollback()
                    self._cleanup_staging(conn, cursor)
                    report.finish("FAILED", f"{type(exc).__name__}: {exc}")
                self._audit_finish(cursor, report)
                conn.commit()
                return report
        finally:
            conn.close()

    # ---- steps ----------------------------------------------------------------------
    def _unchanged(self, cursor, mode: str, sha: str) -> bool:
        last = self.audit.last_success(cursor, self.live)
        if not last or (last.source_sha256, last.load_mode, last.loader_version) != (sha, mode, LOADER_VERSION):
            return False
        if not db.table_exists(cursor, self.live):
            return False
        if mode == "refresh":
            # Guard against the live table having been truncated/edited out of band.
            count = cursor.execute(f"SELECT COUNT_BIG(*) FROM {self.live.qualified};").fetchone()[0]
            return count == last.rows_loaded
        return True

    def _load_staging(self, conn, cursor, report: LoadReport) -> None:
        db.drop_table_if_exists(cursor, self.staging)  # leftover from a killed run
        cursor.execute(create_table_sql(self.staging))
        conn.commit()

        sql = insert_sql(self.staging)
        sink = RejectSink(self.settings.reject_dir, report.run_id)
        for valid in prepare(self.settings, report, sink):
            if not valid.empty:
                db.bulk_insert(cursor, sql, to_rows(valid), self.settings.insert_batch_rows)
                conn.commit()  # staging is private; per-chunk commits keep the log small
        check_quality(report, self.settings.max_reject_ratio)
        self._reconcile(cursor, report)

    def _reconcile(self, cursor, report: LoadReport) -> None:
        count, ts_min, ts_max = cursor.execute(
            f"SELECT COUNT_BIG(*), MIN([{PRIMARY_KEY}]), MAX([{PRIMARY_KEY}]) FROM {self.staging.qualified};"
        ).fetchone()
        expected = (report.rows_loaded, report.ts_min, report.ts_max)
        if (count, ts_min, ts_max) != expected:
            raise ReconciliationError(f"Staging has {(count, ts_min, ts_max)}, expected {expected}")
        logger.info("Reconciled staging: %d rows, %s .. %s", count, ts_min, ts_max)

    def _publish_swap(self, cursor) -> None:
        """Runs inside the caller's transaction; committed once by ``run``."""
        db.drop_table_if_exists(cursor, self.old)
        had_live = db.table_exists(cursor, self.live)
        if had_live:
            cursor.execute("EXEC sp_rename ?, ?;", (self.live.object_name, self.old.name))
        cursor.execute("EXEC sp_rename ?, ?;", (self.staging.object_name, self.live.name))
        if had_live:
            db.drop_table_if_exists(cursor, self.old)
        # Constraint names are schema-scoped; only now that the old table (and its PK_<table>)
        # is gone can the staging constraint names take over the live names.
        stg_names, live_names = constraint_names(self.staging.name), constraint_names(self.live.name)
        for key, stg_name in stg_names.items():
            cursor.execute(
                "EXEC sp_rename ?, ?, N'OBJECT';", (f"{self.live.schema}.{stg_name}", live_names[key])
            )
        logger.info("Swapped %s into %s", self.staging.object_name, self.live.object_name)

    def _publish_merge(self, cursor, report: LoadReport) -> None:
        if not db.table_exists(cursor, self.live):
            cursor.execute(create_table_sql(self.live))
        inserted, updated = cursor.execute(merge_sql(self.live, self.staging)).fetchone()
        report.rows_inserted, report.rows_updated = int(inserted), int(updated)
        db.drop_table_if_exists(cursor, self.staging)
        logger.info("Merged into %s: %d inserted, %d updated", self.live.object_name, inserted, updated)

    def _cleanup_staging(self, conn, cursor) -> None:
        try:
            db.drop_table_if_exists(cursor, self.staging)
            conn.commit()
        except Exception:  # noqa: BLE001 - next run drops it anyway
            logger.warning("Could not drop %s; it will be dropped on the next run.", self.staging.object_name)

    def _audit_start(self, cursor, report: LoadReport, source, sha: str) -> None:
        self.audit.start(
            cursor, run_id=report.run_id, target=self.live, mode=report.mode,
            source_path=str(source), source_sha256=sha, loader_version=LOADER_VERSION, started=_utcnow(),
        )

    def _audit_finish(self, cursor, report: LoadReport) -> None:
        self.audit.finish(
            cursor, run_id=report.run_id, status=report.status, finished=_utcnow(),
            rows_read=report.rows_read, rows_loaded=report.rows_loaded, rows_rejected=report.rows_rejected,
            rows_inserted=report.rows_inserted, rows_updated=report.rows_updated,
            error=report.message if report.status == "FAILED" else None,
        )
