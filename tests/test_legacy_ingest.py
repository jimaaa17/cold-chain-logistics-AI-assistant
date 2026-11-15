"""Offline tests for the legacy MSSQL loader (no database, no ODBC driver needed).

    python -m unittest discover -s tests -v

Set LEGACY_DB_IT=1 to also run the integration test against the MSSQL server in .env.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

from legacy_ingest import cli
from legacy_ingest.config import LegacySettings
from legacy_ingest.db import is_transient_db_error, pick_driver
from legacy_ingest.errors import ConfigError, IngestLocked, ValidationFailed
from legacy_ingest.loader import LoadReport, Loader, check_quality
from legacy_ingest.schema import (
    LOADER_VERSION,
    SOURCE_COLUMNS,
    TARGET_COLUMNS,
    TableRef,
    create_table_sql,
    insert_sql,
    merge_sql,
    quote_ident,
)
from legacy_ingest.transform import to_legacy, to_rows
from legacy_ingest.validate import RejectSink, split_valid

HEADER = ",".join(SOURCE_COLUMNS)


def csv_row(ts="2021-01-01 00:00:00", lat="40.1", lon="-77.0", temp="5.5", cargo="0.7",
            risk="High Risk", delay="0.3", port="4.2", route="1.1") -> str:
    values = dict(zip(SOURCE_COLUMNS, (ts, lat, lon, temp, cargo, risk, delay, port, route)))
    return ",".join(values[c] for c in SOURCE_COLUMNS)


def hourly_rows(n: int) -> list[str]:
    return [csv_row(ts=f"2021-01-{1 + i // 24:02d} {i % 24:02d}:00:00") for i in range(n)]


def raw_frame(*rows: str) -> pd.DataFrame:
    from io import StringIO

    return pd.read_csv(StringIO("\n".join([HEADER, *rows])), dtype=str)


def setUpModule() -> None:
    logging.disable(logging.CRITICAL)


def tearDownModule() -> None:
    logging.disable(logging.NOTSET)


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_csv(self, rows: list[str], name: str = "fleet.csv") -> Path:
        path = self.tmp / name
        path.write_text("\n".join([HEADER, *rows]) + "\n")
        return path

    def settings(self, csv: Path, **overrides) -> LegacySettings:
        values = dict(raw_data_path=csv, reject_dir=self.tmp / "rejects", sql_admin_user="sa",
                      sql_admin_password="pw", csv_chunk_rows=3, insert_batch_rows=2)
        values.update(overrides)
        return LegacySettings(_env_file=None, **values)


# ---- schema -------------------------------------------------------------------------
class SchemaTests(unittest.TestCase):
    def test_identifier_whitelist_blocks_injection(self):
        for bad in ("x]; DROP TABLE y;--", "a b", "1abc", ""):
            with self.assertRaises(ValueError):
                quote_ident(bad)
        with self.assertRaises(ValueError):
            TableRef("dbo", "T]; DROP")

    def test_ddl_types_pk_and_constraint_names_follow_table(self):
        ddl = create_table_sql(TableRef("dbo", "T__STG"))
        self.assertIn("[TS_UTC] DATETIME2(0) NOT NULL CONSTRAINT [PK_T__STG] PRIMARY KEY CLUSTERED", ddl)
        self.assertIn("[RISK_CLS_TXT] VARCHAR(20) NOT NULL", ddl)
        self.assertIn("[SYS_INGEST_FLAG] CHAR(1) NOT NULL CONSTRAINT [DF_T__STG_FLAG] DEFAULT 'Y'", ddl)
        self.assertIn("[CK_T__STG_RISK] CHECK", ddl)
        for col in TARGET_COLUMNS:
            self.assertIn(f"[{col}]", ddl)

    def test_insert_and_merge(self):
        self.assertEqual(insert_sql(TableRef("dbo", "T")).count("?"), len(TARGET_COLUMNS))
        sql = merge_sql(TableRef("dbo", "T"), TableRef("dbo", "T__STG"))
        self.assertIn("ON tgt.[TS_UTC] = src.[TS_UTC]", sql)
        self.assertIn("WITH (HOLDLOCK)", sql)
        self.assertNotIn("tgt.[TS_UTC] = src.[TS_UTC],", sql)  # PK never updated


# ---- transform / validate ------------------------------------------------------------
class TransformValidateTests(unittest.TestCase):
    def test_to_legacy_types_and_flag(self):
        df = to_legacy(raw_frame(csv_row(risk=" Low Risk ")))
        self.assertEqual(list(df.columns), list(TARGET_COLUMNS))
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(df["TS_UTC"]))
        self.assertEqual(df["V_LAT"].dtype, "float64")
        self.assertEqual(df.loc[0, "RISK_CLS_TXT"], "Low Risk")
        self.assertEqual(df.loc[0, "SYS_INGEST_FLAG"], "Y")

    def test_to_rows_produces_plain_python_values(self):
        (row,) = to_rows(to_legacy(raw_frame(csv_row())))
        self.assertIs(type(row[0]), datetime)
        self.assertIs(type(row[1]), float)
        self.assertEqual(row[-1], "Y")

    def test_each_rule_rejects_with_reason(self):
        cases = {
            "TS_UTC": csv_row(ts="not-a-date"),
            "V_LAT": csv_row(lat="95"),
            "V_LON": csv_row(lon="abc"),
            "IOT_TEMP_VAL_C": csv_row(temp=""),
            "CGO_COND_CD": csv_row(cargo="1.5"),
            "DELAY_PROB_DEC": csv_row(delay="-0.1"),
            "PRT_CNG_LVL": csv_row(port="11"),
            "RT_RSK_IDX": csv_row(route="-1"),
            "RISK_CLS_TXT": csv_row(risk="Extreme"),
        }
        for column, row in cases.items():
            with self.subTest(column=column):
                valid, reasons = split_valid(to_legacy(raw_frame(row)), set())
                self.assertTrue(valid.empty)
                self.assertIn(column, reasons.iloc[0])

    def test_duplicates_within_and_across_chunks(self):
        seen: set = set()
        a = csv_row(ts="2021-01-01 00:00:00")
        valid1, rej1 = split_valid(to_legacy(raw_frame(a, a)), seen)
        self.assertEqual((len(valid1), len(rej1)), (1, 1))
        self.assertIn("duplicate", rej1.iloc[0])
        valid2, rej2 = split_valid(to_legacy(raw_frame(a)), seen)
        self.assertEqual((len(valid2), len(rej2)), (0, 1))

    def test_invalid_first_copy_does_not_block_valid_duplicate(self):
        bad = csv_row(ts="2021-01-01 00:00:00", lat="999")
        good = csv_row(ts="2021-01-01 00:00:00")
        valid, reasons = split_valid(to_legacy(raw_frame(bad, good)), set())
        self.assertEqual(len(valid), 1)
        self.assertNotIn("duplicate", reasons.iloc[0])

    def test_reject_ratio_and_empty_load(self):
        report = LoadReport(run_id="r", mode="refresh", rows_read=100, rows_loaded=98, rows_rejected=2)
        with self.assertRaises(ValidationFailed):
            check_quality(report, 0.01)
        check_quality(report, 0.05)
        with self.assertRaises(ValidationFailed):
            check_quality(LoadReport(run_id="r", mode="refresh", rows_read=1, rows_rejected=1), 1.0)

    def test_reject_sink_writes_raw_rows_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = raw_frame(csv_row(lat="999"), csv_row(ts="2021-01-01 01:00:00"))
            _, reasons = split_valid(to_legacy(raw), set())
            sink = RejectSink(Path(tmp), "run1")
            sink.write(raw, reasons)
            out = pd.read_csv(sink.path, dtype=str)
            self.assertEqual(len(out), 1)
            self.assertEqual(out.loc[0, "vehicle_gps_latitude"], "999")
            self.assertIn("V_LAT", out.loc[0, "REJECT_REASON"])


# ---- config / db helpers -------------------------------------------------------------
class ConfigDbTests(unittest.TestCase):
    def test_password_quoted_and_never_in_repr(self):
        s = LegacySettings(_env_file=None, sql_admin_user="sa", sql_admin_password="p;w}d")
        self.assertIn("PWD={p;w}}d};", s.odbc_connection_string("ODBC Driver 18 for SQL Server"))
        self.assertNotIn("p;w}d", repr(s))
        self.assertNotIn("p;w}d", s.describe_target())

    def test_relative_paths_resolve_against_project_root(self):
        s = LegacySettings(_env_file=None, raw_data_path="data/raw/x.csv")
        self.assertTrue(s.raw_data_path.is_absolute())
        self.assertEqual(s.raw_data_path.parts[-3:], ("data", "raw", "x.csv"))

    def test_pick_driver_prefers_newest(self):
        drivers = ["SQLite3", "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"]
        self.assertEqual(pick_driver(drivers), "ODBC Driver 18 for SQL Server")
        with self.assertRaises(ConfigError):
            pick_driver(["SQLite3"])

    def test_transient_sqlstates(self):
        self.assertTrue(is_transient_db_error(Exception("08001", "cannot connect")))
        self.assertTrue(is_transient_db_error(Exception("40001", "deadlock victim")))
        self.assertFalse(is_transient_db_error(Exception("28000", "login failed")))


# ---- loader with a fake pyodbc --------------------------------------------------------
class FakeDB:
    """Just enough SQL Server behaviour (by statement pattern) to drive ``Loader``."""

    _CREATE = re.compile(r"CREATE TABLE \[(\w+)\]\.\[(\w+)\]")
    _DROP = re.compile(r"DROP TABLE IF EXISTS \[(\w+)\]\.\[(\w+)\]")

    def __init__(self, lock_status=0, tables=(), last_success=None, live_count=0, fail_on=None):
        self.lock_status = lock_status
        self.tables = set(tables)
        self.last_success = last_success
        self.live_count = live_count
        self.fail_on = fail_on
        self.log: list[str] = []
        self.staged_rows: list[tuple] = []
        self.audit: dict[str, dict] = {}
        self.closed = False

    def connect(self, settings):
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.db.log.append("COMMIT")

    def rollback(self):
        self.db.log.append("ROLLBACK")

    def close(self):
        self.db.closed = True


class FakeCursor:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self.fast_executemany = False
        self._row = None

    def execute(self, sql, params=()):
        db = self.db
        db.log.append(sql)
        if db.fail_on and db.fail_on in sql:
            raise RuntimeError(f"boom on {db.fail_on}")
        self._row = None
        if "sp_getapplock" in sql:
            self._row = (db.lock_status,)
        elif sql.startswith("SELECT OBJECT_ID"):
            self._row = (1 if params[0] in db.tables else None,)
        elif "SELECT TOP 1 SOURCE_SHA256" in sql:
            self._row = db.last_success
        elif "MIN([TS_UTC])" in sql:
            ts = [r[0] for r in db.staged_rows]
            self._row = (len(ts), min(ts, default=None), max(ts, default=None))
        elif "COUNT_BIG(*)" in sql:
            self._row = (db.live_count,)
        elif "MERGE" in sql:
            self._row = (len(db.staged_rows), 0)
        elif sql.startswith("EXEC sp_rename") and "OBJECT" not in sql:
            old, new = params
            db.tables.discard(old)
            db.tables.add(f"{old.split('.')[0]}.{new}")
        elif sql.startswith("INSERT INTO") and "AUDIT" in sql:
            db.audit[params[0]] = {"status": "RUNNING"}
        elif sql.startswith("UPDATE") and "AUDIT" in sql:
            db.audit[params[-1]] = {"status": params[0], "error": params[7]}
        if m := FakeDB._CREATE.search(sql):
            db.tables.add(f"{m[1]}.{m[2]}")
        if m := FakeDB._DROP.search(sql):
            db.tables.discard(f"{m[1]}.{m[2]}")
        return self

    def executemany(self, sql, rows):
        assert self.fast_executemany
        self.db.log.append("EXECUTEMANY")
        self.db.staged_rows.extend(rows)

    def fetchone(self):
        return self._row


LIVE = "dbo.TBL_SC_FLEET_HIST_RAW"
STG = "dbo.TBL_SC_FLEET_HIST_RAW__STG"


class LoaderTests(TempDirCase):
    def run_loader(self, db: FakeDB, rows=None, mode="refresh", force=False, **overrides):
        csv = self.write_csv(rows if rows is not None else hourly_rows(7))
        return Loader(self.settings(csv, **overrides), db.connect).run(mode=mode, force=force)

    def test_refresh_swaps_atomically(self):
        db = FakeDB(tables={LIVE})
        report = self.run_loader(db)
        self.assertEqual(report.status, "SUCCEEDED", report.message)
        self.assertEqual((report.rows_read, report.rows_loaded), (7, 7))
        self.assertEqual(len(db.staged_rows), 7)
        self.assertEqual(db.log.count("EXECUTEMANY"), 5)  # chunks of 3 -> batches of 2: (2,1)(2,1)(1)
        self.assertIn(LIVE, db.tables)
        self.assertNotIn(STG, db.tables)

        refresh_idx = next(i for i, s in enumerate(db.log) if s.startswith("DELETE FROM [dbo].[TBL_SC_FLEET_HIST_RAW];"))
        drop_stg_idx = max(i for i, s in enumerate(db.log) if "DROP TABLE IF EXISTS [dbo].[TBL_SC_FLEET_HIST_RAW__STG]" in s)
        self.assertNotIn("COMMIT", db.log[refresh_idx:drop_stg_idx])  # one transaction
        self.assertEqual(db.log[drop_stg_idx + 1], "COMMIT")
        self.assertIn("INSERT INTO [dbo].[TBL_SC_FLEET_HIST_RAW]", db.log[refresh_idx])
        self.assertFalse(any(s.startswith("EXEC sp_rename") for s in db.log))
        self.assertEqual(db.audit[report.run_id]["status"], "SUCCEEDED")
        self.assertTrue(db.closed)

    def test_first_refresh_without_live_table(self):
        db = FakeDB()
        report = self.run_loader(db)
        self.assertEqual(report.status, "SUCCEEDED", report.message)
        self.assertTrue(any(s.startswith("CREATE TABLE [dbo].[TBL_SC_FLEET_HIST_RAW] (") for s in db.log))
        self.assertTrue(any(s.startswith("DELETE FROM [dbo].[TBL_SC_FLEET_HIST_RAW];") for s in db.log))
        self.assertIn(LIVE, db.tables)

    def test_failure_rolls_back_drops_staging_and_audits(self):
        db = FakeDB(tables={LIVE}, fail_on="DELETE FROM")
        report = self.run_loader(db)
        self.assertEqual(report.status, "FAILED")
        self.assertIn("boom", report.message)
        self.assertIn("ROLLBACK", db.log)
        self.assertNotIn(STG, db.tables)
        self.assertIn(LIVE, db.tables)
        self.assertEqual(db.audit[report.run_id]["status"], "FAILED")
        self.assertIn("boom", db.audit[report.run_id]["error"])

    def test_too_many_rejects_never_publishes(self):
        db = FakeDB(tables={LIVE})
        rows = hourly_rows(3) + [csv_row(ts="2021-02-01 00:00:00", lat="999")]
        report = self.run_loader(db, rows=rows)
        self.assertEqual(report.status, "FAILED")
        self.assertIn("rejected", report.message)
        self.assertFalse(any(s.startswith("DELETE FROM") for s in db.log))
        self.assertTrue(Path(report.reject_file).is_file())

    def test_unchanged_source_is_skipped(self):
        csv = self.write_csv(hourly_rows(4))
        from legacy_ingest.extract import file_fingerprint

        last = (file_fingerprint(csv), "refresh", LOADER_VERSION, 4)
        db = FakeDB(tables={LIVE}, last_success=last, live_count=4)
        report = Loader(self.settings(csv), db.connect).run()
        self.assertEqual(report.status, "SKIPPED")
        self.assertFalse(db.staged_rows)
        self.assertEqual(db.audit[report.run_id]["status"], "SKIPPED")

        for changed in ({"live_count": 3}, {"tables": set()}):  # tampered or dropped live table
            db = FakeDB(**{"tables": {LIVE}, "last_success": last, "live_count": 4, **changed})
            self.assertEqual(Loader(self.settings(csv), db.connect).run().status, "SUCCEEDED")

        db = FakeDB(tables={LIVE}, last_success=last, live_count=4)
        self.assertEqual(Loader(self.settings(csv), db.connect).run(force=True).status, "SUCCEEDED")

    def test_upsert_merges_and_creates_live_if_missing(self):
        db = FakeDB()
        report = self.run_loader(db, mode="upsert")
        self.assertEqual(report.status, "SUCCEEDED", report.message)
        self.assertEqual((report.rows_inserted, report.rows_updated), (7, 0))
        self.assertTrue(any(s.startswith("CREATE TABLE [dbo].[TBL_SC_FLEET_HIST_RAW] (") for s in db.log))
        self.assertTrue(any("MERGE" in s for s in db.log))
        self.assertFalse(any(s.startswith("EXEC sp_rename") for s in db.log))
        self.assertNotIn(STG, db.tables)

    def test_lock_contention_raises(self):
        db = FakeDB(lock_status=-1)
        with self.assertRaises(IngestLocked):
            self.run_loader(db)
        self.assertFalse(db.staged_rows)
        self.assertTrue(db.closed)


# ---- CLI ------------------------------------------------------------------------------
class CliTests(TempDirCase):
    def run_cli(self, *argv: str) -> int:
        with mock.patch("legacy_ingest.cli.get_legacy_settings",
                        return_value=self.settings(self.tmp / "unused.csv")):
            return cli.main(list(argv))

    def test_dry_run_needs_no_database(self):
        csv = self.write_csv(hourly_rows(5))
        sys.modules.pop("pyodbc", None)
        with mock.patch("legacy_ingest.db.connect", side_effect=AssertionError("connected")):
            self.assertEqual(self.run_cli("--dry-run", "--csv", str(csv)), 0)
        self.assertNotIn("pyodbc", sys.modules)

    def test_dry_run_fails_on_bad_data(self):
        csv = self.write_csv([csv_row(lat="999")])
        self.assertEqual(self.run_cli("--dry-run", "--csv", str(csv)), 1)

    def test_config_errors_exit_2(self):
        self.assertEqual(self.run_cli("--csv", str(self.tmp / "missing.csv")), 2)
        bad_header = self.tmp / "bad.csv"
        bad_header.write_text("a,b\n1,2\n")
        self.assertEqual(self.run_cli("--dry-run", "--csv", str(bad_header)), 2)
        self.assertEqual(self.run_cli("--dry-run", "--max-reject-ratio", "2"), 2)


@unittest.skipUnless(os.getenv("LEGACY_DB_IT") == "1", "set LEGACY_DB_IT=1 to run against MSSQL")
class IntegrationTests(TempDirCase):
    def test_refresh_skip_then_upsert(self):
        from legacy_ingest.db import connect

        csv = self.write_csv(hourly_rows(50))
        base = LegacySettings(raw_data_path=csv, reject_dir=self.tmp / "rejects",
                              target_table="TBL_SC_FLEET_HIST_IT")
        self.assertEqual(Loader(base, connect).run().status, "SUCCEEDED")
        self.assertEqual(Loader(base, connect).run().status, "SKIPPED")
        self.addCleanup(self._drop_it_table, base)
        report = Loader(base, connect).run(mode="upsert", force=True)
        self.assertEqual((report.status, report.rows_inserted, report.rows_updated), ("SUCCEEDED", 0, 0))

    @staticmethod
    def _drop_it_table(settings: LegacySettings) -> None:
        from legacy_ingest.db import connect

        conn = connect(settings)
        conn.cursor().execute(
            "DROP TABLE IF EXISTS dbo.TBL_SC_FLEET_HIST_IT; "
            "DELETE FROM dbo.TBL_SC_INGEST_AUDIT WHERE TABLE_NAME = 'dbo.TBL_SC_FLEET_HIST_IT';"
        )
        conn.commit()
        conn.close()


# ---- Code Review Findings (F-01 .. F-11) & Full Dataset Regression -------------------
class RecordingCursor(FakeCursor):
    _DROP = re.compile(r"DROP TABLE IF EXISTS \[(\w+)\]\.\[(\w+)\]")

    def execute(self, sql, params=()):
        db = self.db
        db.calls.append((sql, params))
        for needle, exc in list(db.fail_once.items()):
            if needle in sql:
                del db.fail_once[needle]
                raise exc
        if m := self._DROP.search(sql):
            db.grants.pop(f"{m[1]}.{m[2]}", None)
        if sql.startswith("EXEC sp_rename") and "OBJECT" not in sql:
            old, new = params
            if old in db.grants:
                db.grants[f"{old.split('.')[0]}.{new}"] = db.grants.pop(old)
        return super().execute(sql, params)

    def executemany(self, sql, rows):
        self.db.executemany_calls += 1
        if self.db.fail_executemany_on == self.db.executemany_calls:
            raise RuntimeError("08S01 communication link failure")
        super().executemany(sql, rows)


class RecordingConnection(FakeConnection):
    def cursor(self):
        return RecordingCursor(self.db)

    def rollback(self):
        super().rollback()
        if self.db.rollback_error:
            raise self.db.rollback_error


class RecordingDB(FakeDB):
    def __init__(self, *, grants=None, fail_once=None, fail_executemany_on=None, rollback_error=None, **kw):
        super().__init__(**kw)
        self.calls: list[tuple[str, tuple]] = []
        self.grants: dict[str, set[str]] = grants or {}
        self.fail_once: dict[str, Exception] = fail_once or {}
        self.fail_executemany_on = fail_executemany_on
        self.executemany_calls = 0
        self.rollback_error = rollback_error

    def connect(self, settings):
        return RecordingConnection(self)

    def audit_insert_params(self) -> tuple:
        return next(p for s, p in self.calls if s.startswith("INSERT INTO") and "AUDIT" in s)


class RegressionFindingsTests(TempDirCase):
    def load(self, db, rows=None, mode="refresh", **overrides):
        csv = self.write_csv(rows if rows is not None else hourly_rows(7))
        return Loader(self.settings(csv, **overrides), db.connect).run(mode=mode)

    def test_F01_refresh_preserves_permissions_on_live_table(self):
        db = RecordingDB(tables={LIVE}, grants={LIVE: {"SELECT TO USR_FDE_RO"}})
        report = self.load(db)
        self.assertEqual(report.status, "SUCCEEDED", report.message)
        self.assertEqual(db.grants.get(LIVE), {"SELECT TO USR_FDE_RO"})

    def test_F02_dead_connection_during_failure_handling_still_returns_failed_report(self):
        db = RecordingDB(tables={LIVE}, fail_executemany_on=1,
                         rollback_error=RuntimeError("08S01 rollback on dead connection"))
        report = self.load(db)
        self.assertEqual(report.status, "FAILED")
        self.assertIn("communication link failure", report.message)

    def test_F03_deadlock_victim_during_publish_is_retried(self):
        db = RecordingDB(tables={LIVE}, fail_once={"MERGE": Exception("40001", "deadlock victim")})
        report = self.load(db, mode="upsert")
        self.assertEqual(report.status, "SUCCEEDED", report.message)

    def test_F04_upsert_does_not_skip_when_live_table_was_emptied(self):
        from legacy_ingest.extract import file_fingerprint

        csv = self.write_csv(hourly_rows(4))
        last = (file_fingerprint(csv), "upsert", LOADER_VERSION, 4)
        db = RecordingDB(tables={LIVE}, last_success=last, live_count=0)
        report = Loader(self.settings(csv), db.connect).run(mode="upsert")
        self.assertNotEqual(report.status, "SKIPPED")

    def test_F05_audit_hash_matches_the_bytes_actually_loaded(self):
        import hashlib
        import legacy_ingest.loader as loader_mod

        csv = self.write_csv(hourly_rows(4))
        replacement = "\n".join([",".join(SOURCE_COLUMNS), *hourly_rows(5)]) + "\n"
        original = loader_mod.read_chunks

        def swapped_then_read(path, chunk_rows):
            Path(path).write_text(replacement)
            return original(path, chunk_rows)

        db = RecordingDB()
        with mock.patch.object(loader_mod, "read_chunks", swapped_then_read):
            report = Loader(self.settings(csv), db.connect).run()
        self.assertEqual(report.status, "SUCCEEDED", report.message)
        self.assertEqual(report.rows_loaded, 5)
        self.assertEqual(db.audit_insert_params()[4], hashlib.sha256(replacement.encode()).hexdigest())

    def test_F06_csv_missing_column_is_config_error_exit_2(self):
        csv = self.tmp / "bad.csv"
        csv.write_text(",".join(c for c in SOURCE_COLUMNS if c != "iot_temperature") + "\n")
        db = RecordingDB()
        with mock.patch("legacy_ingest.cli.get_legacy_settings", return_value=self.settings(csv)), \
             mock.patch("legacy_ingest.db.connect", db.connect):
            code = cli.main(["--csv", str(csv)])
        self.assertEqual(code, 2)
        self.assertFalse(any("CREATE TABLE [dbo].[TBL_SC_FLEET_HIST_RAW__STG]" in s for s, _ in db.calls))

    def test_F07_legacy_ingest_does_not_depend_on_rag(self):
        import ast
        from legacy_ingest.config import PROJECT_ROOT

        offenders = []
        for path in sorted((PROJECT_ROOT / "BE" / "legacy_ingest").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                        [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                offenders += [f"{path.name}: {n}" for n in names if n == "rag" or n.startswith("rag.")]
        self.assertEqual(offenders, [])

    def test_F08_failed_run_still_reports_written_reject_file(self):
        rows = [csv_row(ts="2021-01-01 00:00:00", lat="999"), *hourly_rows(6)]
        db = RecordingDB(fail_executemany_on=3)
        report = self.load(db, rows=rows, max_reject_ratio=1.0)
        self.assertEqual(report.status, "FAILED")
        reject_file = self.tmp / "rejects" / f"{report.run_id}.csv"
        self.assertTrue(reject_file.is_file())
        self.assertEqual(report.reject_file, str(reject_file))

    def test_F09_chunk_rows_zero_is_rejected(self):
        csv = self.write_csv(hourly_rows(3))
        with mock.patch("legacy_ingest.cli.get_legacy_settings", return_value=self.settings(csv)):
            self.assertEqual(cli.main(["--dry-run", "--chunk-rows", "-1"]), 2)
            self.assertEqual(cli.main(["--dry-run", "--chunk-rows", "0"]), 2)

    def test_F10_identifier_with_trailing_newline_is_rejected(self):
        with self.assertRaises(ValueError):
            quote_ident("TBL_SC_FLEET_HIST_RAW\n")
        with self.assertRaises(ValueError):
            TableRef("dbo", "TBL_SC_FLEET_HIST_RAW\n")

    def test_F11_source_csv_is_parsed_once(self):
        import legacy_ingest.extract as extract_mod

        csv = self.write_csv(hourly_rows(7))
        with mock.patch.object(extract_mod.pd, "read_csv", wraps=extract_mod.pd.read_csv) as read_csv:
            report = Loader(self.settings(csv), RecordingDB().connect).run()
        self.assertEqual(report.status, "SUCCEEDED", report.message)
        self.assertEqual(read_csv.call_count, 1)


@unittest.skipUnless(
    (Path(__file__).resolve().parent.parent / "data" / "raw" / "dynamic_supply_chain_logistics_dataset.csv").is_file(),
    "real dataset not present in data/raw/",
)
class RealDatasetRegressionTests(TempDirCase):
    """End-to-end regression suite against data/raw/dynamic_supply_chain_logistics_dataset.csv."""

    @property
    def dataset_path(self) -> Path:
        from legacy_ingest.config import DEFAULT_DATA_FILE

        return DEFAULT_DATA_FILE

    def real_settings(self, **overrides) -> LegacySettings:
        values = dict(
            raw_data_path=self.dataset_path,
            reject_dir=self.tmp / "rejects",
            sql_admin_user="sa",
            sql_admin_password="pw",
            csv_chunk_rows=10_000,
            insert_batch_rows=5_000,
        )
        values.update(overrides)
        return LegacySettings(_env_file=None, **values)

    def test_real_dataset_dry_run_validates_all_32065_rows(self):
        from legacy_ingest.loader import dry_run

        report = dry_run(self.real_settings())
        self.assertEqual(report.status, "DRY_RUN")
        self.assertTrue(report.ok)
        self.assertEqual(report.rows_read, 32_065)
        self.assertEqual(report.rows_loaded, 32_065)
        self.assertEqual(report.rows_rejected, 0)
        self.assertIsNone(report.reject_file)
        self.assertEqual(report.ts_min, datetime(2021, 1, 1, 0, 0, 0))
        self.assertEqual(report.ts_max, datetime(2024, 8, 29, 0, 0, 0))

    def test_real_dataset_matches_legacy_mapping_contract(self):
        from legacy_ingest.extract import read_chunks
        from legacy_ingest.schema import LEGACY_MAPPING, NUMERIC_COLUMNS

        # Reference DataFrame produced using scripts/ingest_legacy_data.py's exact mapping
        raw_df = pd.read_csv(self.dataset_path)
        ref_df = raw_df[list(LEGACY_MAPPING.keys())].rename(columns=LEGACY_MAPPING)
        ref_df["TS_UTC"] = pd.to_datetime(ref_df["TS_UTC"])
        ref_df["SYS_INGEST_FLAG"] = "Y"

        # Output produced by the refactored chunked pipeline
        seen: set = set()
        frames = []
        for chunk in read_chunks(self.dataset_path, 10_000):
            valid, reasons = split_valid(to_legacy(chunk), seen)
            self.assertTrue(reasons.empty)
            frames.append(valid)
        pipeline_df = pd.concat(frames, ignore_index=True)

        self.assertEqual(len(pipeline_df), len(ref_df))
        self.assertEqual(list(pipeline_df.columns), list(TARGET_COLUMNS))
        self.assertEqual(pipeline_df["TS_UTC"].iloc[0], ref_df["TS_UTC"].iloc[0])
        self.assertEqual(pipeline_df["TS_UTC"].iloc[-1], ref_df["TS_UTC"].iloc[-1])
        self.assertEqual(
            pipeline_df["RISK_CLS_TXT"].value_counts().to_dict(),
            ref_df["RISK_CLS_TXT"].value_counts().to_dict(),
        )
        for col in NUMERIC_COLUMNS:
            self.assertAlmostEqual(float(pipeline_df[col].sum()), float(ref_df[col].sum()), places=5, msg=col)
        self.assertTrue((pipeline_df["SYS_INGEST_FLAG"] == "Y").all())

    def test_real_dataset_refresh_skip_and_upsert_lifecycle(self):
        import legacy_ingest.extract as extract_mod
        from legacy_ingest.extract import file_fingerprint

        sha = file_fingerprint(self.dataset_path)
        settings = self.real_settings()

        # 1. Full refresh on existing live table preserves grants and parses CSV once
        db1 = RecordingDB(tables={LIVE}, grants={LIVE: {"SELECT TO USR_FDE_RO"}})
        with mock.patch.object(extract_mod.pd, "read_csv", wraps=extract_mod.pd.read_csv) as read_csv:
            r1 = Loader(settings, db1.connect).run(mode="refresh")
        self.assertEqual(r1.status, "SUCCEEDED", r1.message)
        self.assertEqual((r1.rows_read, r1.rows_loaded, r1.rows_rejected), (32_065, 32_065, 0))
        self.assertEqual(len(db1.staged_rows), 32_065)
        self.assertEqual(db1.executemany_calls, 7)  # chunks (10k,10k,10k,2065) -> batches of 5k: 2+2+2+1 = 7
        self.assertEqual(db1.grants.get(LIVE), {"SELECT TO USR_FDE_RO"})
        self.assertEqual(read_csv.call_count, 1)
        self.assertEqual(db1.audit_insert_params()[4], sha)

        # 2. Second refresh with unchanged CSV is SKIPPED
        last_refresh = (sha, "refresh", LOADER_VERSION, 32_065)
        db2 = RecordingDB(tables={LIVE}, last_success=last_refresh, live_count=32_065)
        r2 = Loader(settings, db2.connect).run(mode="refresh")
        self.assertEqual(r2.status, "SKIPPED")
        self.assertEqual(len(db2.staged_rows), 0)

        # 3. Upsert mode loads and merges all 32,065 rows
        db3 = RecordingDB(tables={LIVE})
        r3 = Loader(settings, db3.connect).run(mode="upsert")
        self.assertEqual(r3.status, "SUCCEEDED", r3.message)
        self.assertEqual((r3.rows_inserted, r3.rows_updated), (32_065, 0))

        # 4. Upsert does NOT skip if live table was emptied out of band
        last_upsert = (sha, "upsert", LOADER_VERSION, 32_065)
        db4 = RecordingDB(tables={LIVE}, last_success=last_upsert, live_count=0)
        r4 = Loader(settings, db4.connect).run(mode="upsert")
        self.assertEqual(r4.status, "SUCCEEDED")
        self.assertEqual(r4.rows_loaded, 32_065)


if __name__ == "__main__":
    unittest.main()
