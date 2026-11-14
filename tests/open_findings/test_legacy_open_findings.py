"""Reproduction tests for the OPEN findings in
materials/code-review/legacy-data-ingestion-code-review.pdf.

Every test asserts the CORRECT behaviour, so each one FAILS on today's code and should pass
once its finding is fixed. When a finding is fixed, move its test into
tests/test_legacy_ingest.py. This folder has no __init__.py on purpose, so
`python -m unittest discover -s tests` stays green; run these explicitly:

    venv/bin/python -m unittest discover -s tests/open_findings -v
"""
from __future__ import annotations

import hashlib
import logging
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "tests")]

from test_legacy_ingest import (  # noqa: E402
    LIVE,
    FakeConnection,
    FakeCursor,
    FakeDB,
    TempDirCase,
    csv_row,
    hourly_rows,
)

from legacy_ingest import cli  # noqa: E402
from legacy_ingest.extract import file_fingerprint  # noqa: E402
from legacy_ingest.loader import Loader  # noqa: E402
from legacy_ingest.schema import LOADER_VERSION, SOURCE_COLUMNS, TableRef, quote_ident  # noqa: E402


def setUpModule() -> None:
    logging.disable(logging.CRITICAL)


def tearDownModule() -> None:
    logging.disable(logging.NOTSET)


# ---- a FakeDB that also models grants, parameters and one-off failures ---------------
class RecordingCursor(FakeCursor):
    _DROP = re.compile(r"DROP TABLE IF EXISTS \[(\w+)\]\.\[(\w+)\]")

    def execute(self, sql, params=()):
        db = self.db
        db.calls.append((sql, params))
        for needle, exc in list(db.fail_once.items()):
            if needle in sql:
                del db.fail_once[needle]
                raise exc
        if m := self._DROP.search(sql):  # object-level permissions die with the object
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


class LegacyOpenFindings(TempDirCase):
    def load(self, db, rows=None, mode="refresh", **overrides):
        csv = self.write_csv(rows if rows is not None else hourly_rows(7))
        return Loader(self.settings(csv, **overrides), db.connect).run(mode=mode)

    def test_F01_refresh_preserves_permissions_on_live_table(self):
        """GRANTs (and indexes, triggers, dependent views) on the live table survive a refresh."""
        db = RecordingDB(tables={LIVE}, grants={LIVE: {"SELECT TO USR_FDE_RO"}})
        report = self.load(db)
        self.assertEqual(report.status, "SUCCEEDED", report.message)
        self.assertEqual(db.grants.get(LIVE), {"SELECT TO USR_FDE_RO"},
                         "refresh dropped the GRANT the agent's read-only login depends on")

    def test_F02_dead_connection_during_failure_handling_still_returns_failed_report(self):
        """If rollback itself fails (link dropped), run() still returns a FAILED report with the root cause."""
        db = RecordingDB(tables={LIVE}, fail_executemany_on=1,
                         rollback_error=RuntimeError("08S01 rollback on dead connection"))
        try:
            report = self.load(db)
        except RuntimeError as exc:
            self.fail(f"run() raised from the failure handler instead of returning FAILED: {exc}")
        self.assertEqual(report.status, "FAILED")
        self.assertIn("communication link failure", report.message)

    def test_F03_deadlock_victim_during_publish_is_retried(self):
        """A transient 40001 deadlock during MERGE is retried, as TRANSIENT_SQLSTATES implies."""
        db = RecordingDB(tables={LIVE}, fail_once={"MERGE": Exception("40001", "deadlock victim")})
        report = self.load(db, mode="upsert")
        self.assertEqual(report.status, "SUCCEEDED", f"deadlock not retried: {report.message}")

    def test_F04_upsert_does_not_skip_when_live_table_was_emptied(self):
        """An unchanged CSV is reloaded in upsert mode when the live table no longer holds its rows."""
        csv = self.write_csv(hourly_rows(4))
        last = (file_fingerprint(csv), "upsert", LOADER_VERSION, 4)
        db = RecordingDB(tables={LIVE}, last_success=last, live_count=0)  # truncated out of band
        report = Loader(self.settings(csv), db.connect).run(mode="upsert")
        self.assertNotEqual(report.status, "SKIPPED", "upsert skipped although the live table is empty")

    def test_F05_audit_hash_matches_the_bytes_actually_loaded(self):
        """SOURCE_SHA256 in the audit row describes the file content that was loaded."""
        csv = self.write_csv(hourly_rows(4))
        replacement = "\n".join([",".join(SOURCE_COLUMNS), *hourly_rows(5)]) + "\n"
        import legacy_ingest.loader as loader_mod

        original = loader_mod.read_chunks

        def swapped_then_read(path, chunk_rows):  # file replaced between hashing and parsing
            Path(path).write_text(replacement)
            return original(path, chunk_rows)

        db = RecordingDB()
        with mock.patch.object(loader_mod, "read_chunks", swapped_then_read):
            report = Loader(self.settings(csv), db.connect).run()
        self.assertEqual(report.status, "SUCCEEDED", report.message)
        self.assertEqual(report.rows_loaded, 5)
        self.assertEqual(db.audit_insert_params()[4], hashlib.sha256(replacement.encode()).hexdigest(),
                         "audit recorded the hash of a file version that was not loaded")

    def test_F06_csv_missing_column_is_config_error_exit_2(self):
        """A CSV missing a mapped column exits 2 (config error) before any DB work, as in --dry-run."""
        csv = self.tmp / "bad.csv"
        csv.write_text(",".join(c for c in SOURCE_COLUMNS if c != "iot_temperature") + "\n")
        db = RecordingDB()
        with mock.patch("legacy_ingest.cli.get_legacy_settings", return_value=self.settings(csv)), \
             mock.patch("legacy_ingest.db.connect", db.connect):
            code = cli.main(["--csv", str(csv)])
        self.assertEqual(code, 2, "missing CSV column reported as a load failure (exit 1)")
        self.assertFalse(any("CREATE TABLE [dbo].[TBL_SC_FLEET_HIST_RAW__STG]" in s for s, _ in db.calls),
                         "staging table was created before the CSV header was checked")

    def test_F07_legacy_ingest_does_not_depend_on_rag(self):
        """The loader is deployable without the rag/ package (no imports of rag.*)."""
        import ast

        offenders = []
        for path in sorted((REPO / "legacy_ingest").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                        [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                offenders += [f"{path.name}: {n}" for n in names if n == "rag" or n.startswith("rag.")]
        self.assertEqual(offenders, [], "legacy_ingest imports the rag package")

    def test_F08_failed_run_still_reports_written_reject_file(self):
        """When a load fails mid-stream, the quarantine CSV already written is still reported."""
        rows = [csv_row(ts="2021-01-01 00:00:00", lat="999"), *hourly_rows(6)]
        db = RecordingDB(fail_executemany_on=3)  # chunk 1 (with the reject) is fully written first
        report = self.load(db, rows=rows, max_reject_ratio=1.0)
        self.assertEqual(report.status, "FAILED")
        reject_file = self.tmp / "rejects" / f"{report.run_id}.csv"
        self.assertTrue(reject_file.is_file(), "precondition: rejects were written to disk")
        self.assertEqual(report.reject_file, str(reject_file), "FAILED report hides the reject file")

    def test_F09_chunk_rows_zero_is_rejected(self):
        """--chunk-rows 0 fails validation (exit 2) like --chunk-rows -1, instead of being ignored."""
        csv = self.write_csv(hourly_rows(3))
        with mock.patch("legacy_ingest.cli.get_legacy_settings", return_value=self.settings(csv)):
            self.assertEqual(cli.main(["--dry-run", "--chunk-rows", "-1"]), 2)  # control: works today
            self.assertEqual(cli.main(["--dry-run", "--chunk-rows", "0"]), 2, "--chunk-rows 0 silently ignored")

    def test_F10_identifier_with_trailing_newline_is_rejected(self):
        """Config-supplied table names ending in a newline fail the identifier whitelist."""
        with self.assertRaises(ValueError, msg="quote_ident accepted 'TBL\\n'"):
            quote_ident("TBL_SC_FLEET_HIST_RAW\n")
        with self.assertRaises(ValueError):
            TableRef("dbo", "TBL_SC_FLEET_HIST_RAW\n")

    def test_F11_source_csv_is_read_in_a_single_pass(self):
        """Hashing, header check and parsing share one read of the source file."""
        import legacy_ingest.extract as extract_mod
        import legacy_ingest.loader as loader_mod

        csv = self.write_csv(hourly_rows(7))
        with mock.patch.object(extract_mod.pd, "read_csv", wraps=extract_mod.pd.read_csv) as read_csv, \
             mock.patch.object(loader_mod, "file_fingerprint", wraps=file_fingerprint) as fingerprint:
            report = Loader(self.settings(csv), RecordingDB().connect).run()
        self.assertEqual(report.status, "SUCCEEDED", report.message)
        passes = read_csv.call_count + fingerprint.call_count
        self.assertEqual(passes, 1, f"source file read {passes} times per load")


if __name__ == "__main__":
    unittest.main()
