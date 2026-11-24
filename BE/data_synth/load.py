"""Loads or clears the synthetic table, through the unmodified ``legacy_ingest`` loader.

Only ever touches ``SYNTH_TABLE``; the historical table is never a target. Refuses a remote SQL host or
``APP_ENV=production`` unless told otherwise. (On a server whose database is local, the host check alone
can't tell it from a laptop, so production hosts should set ``APP_ENV=production``.)
"""
from __future__ import annotations

import os
from pathlib import Path

from data_synth.config import SYNTH_TABLE
from legacy_ingest.config import LegacySettings, get_legacy_settings

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class UnsafeTarget(RuntimeError):
    pass


def synth_settings(csv_path: Path | None = None, *, base: LegacySettings | None = None) -> LegacySettings:
    settings = base or get_legacy_settings()
    overrides: dict = {"target_table": SYNTH_TABLE}
    if csv_path is not None:
        overrides["raw_data_path"] = csv_path.resolve()
    return type(settings).model_validate({**settings.model_dump(), **overrides})


def check_target(settings: LegacySettings, *, allow_remote: bool) -> None:
    if os.getenv("APP_ENV", "").strip().lower() in {"production", "prod"}:
        raise UnsafeTarget("APP_ENV is production; synthetic data is for development only.")
    if not allow_remote and settings.sql_server_host.strip().lower() not in _LOCAL_HOSTS:
        raise UnsafeTarget(f"SQL host {settings.sql_server_host!r} is not local. Use --allow-remote only for a development server.")


def load_csv(csv_path: Path, *, allow_remote: bool = False, connect=None, base: LegacySettings | None = None):
    from legacy_ingest import db
    from legacy_ingest.loader import Loader

    settings = synth_settings(csv_path, base=base)
    check_target(settings, allow_remote=allow_remote)
    # force: the loader skips a file it has already loaded, but the table may have been cleared since
    return Loader(settings, connect or db.connect).run(mode="refresh", force=True)


def clear(*, allow_remote: bool = False, connect=None, base: LegacySettings | None = None) -> int:
    """Deletes every synthetic row; returns how many there were."""
    from legacy_ingest import db
    from legacy_ingest.schema import TableRef

    settings = synth_settings(base=base)
    check_target(settings, allow_remote=allow_remote)
    table = TableRef(settings.target_schema, SYNTH_TABLE)
    conn = (connect or db.connect)(settings)
    try:
        cursor = conn.cursor()
        if not db.table_exists(cursor, table):
            return 0
        removed = cursor.execute(f"DELETE FROM {table.qualified};").rowcount
        conn.commit()
        return int(removed)
    finally:
        conn.close()
