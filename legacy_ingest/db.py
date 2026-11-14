"""pyodbc access: connection with retries, fast bulk insert, and a cross-process load lock.

``pyodbc`` is imported lazily so ``--help`` / ``--dry-run`` work without the ODBC stack.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from legacy_ingest.config import LegacySettings
from legacy_ingest.errors import ConfigError, IngestLocked
from legacy_ingest.schema import TableRef
from rag.core.resilience import is_transient, with_retry

logger = logging.getLogger(__name__)

# Connection failures, link drops, timeouts and deadlock victims are worth retrying.
# Login failures (bad credentials) come back as 28000 and fail fast.
TRANSIENT_SQLSTATES = frozenset({"08001", "08S01", "HYT00", "HYT01", "40001"})
_DRIVER_NAME = re.compile(r"^ODBC Driver (\d+) for SQL Server$")


def is_transient_db_error(exc: BaseException) -> bool:
    sqlstate = exc.args[0] if getattr(exc, "args", None) else None
    if isinstance(sqlstate, str) and sqlstate in TRANSIENT_SQLSTATES:
        return True
    return is_transient(exc)


def pick_driver(available: Sequence[str]) -> str:
    """Newest installed Microsoft ODBC driver for SQL Server."""
    versions = sorted(
        (int(match.group(1)), name) for name in available if (match := _DRIVER_NAME.match(name))
    )
    if not versions:
        raise ConfigError(
            "No 'ODBC Driver NN for SQL Server' installed. Install ODBC Driver 18 "
            "(macOS: brew install msodbcsql18) or set SQL_ODBC_DRIVER."
        )
    return versions[-1][1]


def connect(settings: LegacySettings):
    import pyodbc

    if not settings.sql_admin_user or not settings.sql_admin_password:
        raise ConfigError("SQL_ADMIN_USER and SQL_ADMIN_PASSWORD must be set (env or .env).")
    driver = settings.sql_odbc_driver or pick_driver(pyodbc.drivers())
    conn_str = settings.odbc_connection_string(driver)

    @with_retry(attempts=settings.sql_connect_attempts, predicate=is_transient_db_error)
    def _connect():
        return pyodbc.connect(conn_str, autocommit=False, timeout=settings.sql_connect_timeout)

    logger.info("Connecting to %s via %r", settings.describe_target(), driver)
    return _connect()


def bulk_insert(cursor, sql: str, rows: Sequence[tuple], batch_rows: int) -> None:
    # fast_executemany sends each batch as one parameter array instead of a round trip per row.
    cursor.fast_executemany = True
    for start in range(0, len(rows), batch_rows):
        cursor.executemany(sql, rows[start:start + batch_rows])


def table_exists(cursor, table: TableRef) -> bool:
    return cursor.execute("SELECT OBJECT_ID(?, N'U');", (table.object_name,)).fetchone()[0] is not None


def drop_table_if_exists(cursor, table: TableRef) -> None:
    cursor.execute(f"DROP TABLE IF EXISTS {table.qualified};")


@contextmanager
def app_lock(conn, resource: str) -> Iterator[None]:
    """Session-scoped ``sp_getapplock``: a second concurrent run fails fast with IngestLocked.
    SQL Server releases the lock automatically if this process dies."""
    cursor = conn.cursor()
    status = cursor.execute(
        "SET NOCOUNT ON; DECLARE @rc INT; "
        "EXEC @rc = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
        "@LockOwner = 'Session', @LockTimeout = 0; SELECT @rc;",
        (resource,),
    ).fetchone()[0]
    conn.commit()
    if status < 0:
        raise IngestLocked(f"Another load holds lock {resource!r} (sp_getapplock={status}).")
    try:
        yield
    finally:
        try:
            conn.rollback()  # never release while a failed transaction is still open
            cursor.execute(
                "EXEC sp_releaseapplock @Resource = ?, @LockOwner = 'Session';", (resource,)
            )
            conn.commit()
        except Exception:  # noqa: BLE001 - connection may already be gone; lock dies with it
            logger.debug("Lock release failed; it is freed when the session closes.", exc_info=True)
