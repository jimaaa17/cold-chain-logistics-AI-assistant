"""Run audit table: one row per load attempt, also used for idempotent skips."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from legacy_ingest.schema import AUDIT_TABLE, TableRef

_MAX_ERROR_CHARS = 4000


@dataclass(frozen=True)
class LastSuccess:
    source_sha256: str
    load_mode: str
    loader_version: str
    rows_loaded: int


class AuditLog:
    def __init__(self, schema: str) -> None:
        self.table = TableRef(schema, AUDIT_TABLE)

    def ensure(self, cursor) -> None:
        cursor.execute(
            f"IF OBJECT_ID(?, N'U') IS NULL\n"
            f"CREATE TABLE {self.table.qualified} (\n"
            f"    RUN_ID UNIQUEIDENTIFIER NOT NULL CONSTRAINT [PK_{AUDIT_TABLE}] PRIMARY KEY,\n"
            "    TABLE_NAME NVARCHAR(256) NOT NULL,\n"
            "    LOAD_MODE VARCHAR(10) NOT NULL,\n"
            "    SOURCE_PATH NVARCHAR(1024) NOT NULL,\n"
            "    SOURCE_SHA256 CHAR(64) NOT NULL,\n"
            "    LOADER_VERSION VARCHAR(16) NOT NULL,\n"
            "    STARTED_UTC DATETIME2(3) NOT NULL,\n"
            "    FINISHED_UTC DATETIME2(3) NULL,\n"
            "    ROWS_READ INT NULL,\n"
            "    ROWS_LOADED INT NULL,\n"
            "    ROWS_REJECTED INT NULL,\n"
            "    ROWS_INSERTED INT NULL,\n"
            "    ROWS_UPDATED INT NULL,\n"
            "    STATUS VARCHAR(10) NOT NULL,\n"
            "    ERROR_TEXT NVARCHAR(4000) NULL\n"
            ");",
            (self.table.object_name,),
        )

    def last_success(self, cursor, target: TableRef) -> LastSuccess | None:
        row = cursor.execute(
            f"SELECT TOP 1 SOURCE_SHA256, LOAD_MODE, LOADER_VERSION, ROWS_LOADED "
            f"FROM {self.table.qualified} WHERE TABLE_NAME = ? AND STATUS = 'SUCCEEDED' "
            "ORDER BY FINISHED_UTC DESC;",
            (target.object_name,),
        ).fetchone()
        return LastSuccess(*row) if row else None

    def start(self, cursor, *, run_id: str, target: TableRef, mode: str, source_path: str,
              source_sha256: str, loader_version: str, started: datetime) -> None:
        cursor.execute(
            f"INSERT INTO {self.table.qualified} (RUN_ID, TABLE_NAME, LOAD_MODE, SOURCE_PATH, "
            "SOURCE_SHA256, LOADER_VERSION, STARTED_UTC, STATUS) VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING');",
            (run_id, target.object_name, mode, source_path, source_sha256, loader_version, started),
        )

    def finish(self, cursor, *, run_id: str, status: str, finished: datetime, rows_read: int,
               rows_loaded: int, rows_rejected: int, rows_inserted: int | None,
               rows_updated: int | None, error: str | None = None) -> None:
        cursor.execute(
            f"UPDATE {self.table.qualified} SET STATUS = ?, FINISHED_UTC = ?, ROWS_READ = ?, "
            "ROWS_LOADED = ?, ROWS_REJECTED = ?, ROWS_INSERTED = ?, ROWS_UPDATED = ?, ERROR_TEXT = ? "
            "WHERE RUN_ID = ?;",
            (status, finished, rows_read, rows_loaded, rows_rejected, rows_inserted, rows_updated,
             error[:_MAX_ERROR_CHARS] if error else None, run_id),
        )
