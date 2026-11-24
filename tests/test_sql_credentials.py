"""No password ships with the repo: settings require one, the setup files hold none, and rotation is safe."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.config import AgentSettings, MissingSqlPasswordError

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rotate_agent_password", ROOT / "scripts" / "rotate_agent_password.py")
rotate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rotate)

ENV_TEXT = (
    "SQL_ADMIN_USER=sa\nSQL_ADMIN_PASSWORD=admin-secret\nSQL_SERVER_HOST=db.internal\n"
    "SQL_AGENT_USER=USR_FDE_RO\nSQL_AGENT_PASSWORD=old-secret\nAgent_llm=GEMINI\n"
)


class TestRequiredPassword(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ")
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("SQL_AGENT_PASSWORD", None)

    def test_missing_or_empty_password_is_a_clear_error(self):
        for settings in (AgentSettings(_env_file=None), AgentSettings(_env_file=None, sql_agent_password="")):
            with self.assertRaisesRegex(MissingSqlPasswordError, "SQL_AGENT_PASSWORD"):
                settings.odbc_connection_string()

    def test_configured_password_is_used_and_escaped(self):
        settings = AgentSettings(_env_file=None, sql_agent_password="p;w}d")
        self.assertIn("PWD={p;w}}d};", settings.odbc_connection_string())

    def test_meta_reports_the_missing_password(self):
        import api.server as server

        with mock.patch("agent.config.get_agent_settings", return_value=AgentSettings(_env_file=None)):
            errors = server.build_meta_payload().get("config_errors", [])
        self.assertTrue(any(e.startswith("agent: ") and "SQL_AGENT_PASSWORD" in e for e in errors), errors)

    def test_dry_run_fails_without_a_password(self):
        from agent import cli

        with mock.patch.object(cli, "get_agent_settings", return_value=AgentSettings(_env_file=None)):
            self.assertEqual(cli.main(["--dry-run"]), 2)


class TestNoCommittedPasswords(unittest.TestCase):
    def test_setup_scripts_match_and_hold_no_login_password(self):
        be = (ROOT / "BE/sql/setup_security_view.sql").read_text(encoding="utf-8")
        self.assertEqual(be, (ROOT / "scripts/setup_security_view.sql").read_text(encoding="utf-8"))
        self.assertIsNone(re.search(r"PASSWORD\s*=\s*N?'[^']", be, re.IGNORECASE))
        self.assertIn("CRYPT_GEN_RANDOM", be)

    def test_docs_hold_placeholders_only(self):
        for path in (ROOT / "docs").rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for value in re.findall(r"MSSQL_SA_PASSWORD=([^\"\s]+)", text):
                self.assertTrue(value.startswith(("<", "$")), f"{path}: literal sa password")
            for value in re.findall(r"(?m)^[\s*]*Password\*:[ \t]*(.+)$", text):
                self.assertTrue(value.startswith("the value of SQL_"), f"{path}: literal profile password")


class _FakeConnection:
    def __init__(self, server, settings):
        self.server, self.settings, self.autocommit, self.closed = server, settings, False, False

    def cursor(self):
        return self

    def execute(self, sql):
        self.server.statements.append(sql)
        if sql.startswith("ALTER") and self.server.fail_alter:
            raise RuntimeError("42000", f"near {sql}")
        return self

    def fetchone(self):
        return (1,)

    def close(self):
        self.closed = True


class _FakeServer:
    def __init__(self, *, fail_admin=False, fail_alter=False, fail_agent=False):
        self.fail_admin, self.fail_alter, self.fail_agent = fail_admin, fail_alter, fail_agent
        self.statements, self.connections = [], []

    def connect(self, settings):
        if self.fail_admin or (self.fail_agent and settings.sql_admin_user == "USR_FDE_RO"):
            raise RuntimeError("28000", "Login failed")
        conn = _FakeConnection(self, settings)
        self.connections.append(conn)
        return conn


class TestRotation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = Path(tmp.name).resolve() / ".env"
        self.env.write_text(ENV_TEXT, encoding="utf-8")
        self.pending = self.env.with_name(".env.rotating")
        patcher = mock.patch.dict("os.environ")
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [n for n in os.environ if n.upper().startswith("SQL_")]:
            del os.environ[name]

    def run_script(self, *argv, server):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = rotate.main(["--env-file", str(self.env), *argv], connect=server.connect)
        return code, out.getvalue()

    def new_password(self, path):
        return re.search(r"(?m)^SQL_AGENT_PASSWORD=(.*)$", path.read_text(encoding="utf-8")).group(1)

    def test_without_yes_nothing_changes(self):
        server = _FakeServer()
        code, out = self.run_script(server=server)
        self.assertEqual((code, server.connections), (0, []))
        self.assertEqual(self.env.read_text(encoding="utf-8"), ENV_TEXT)
        self.assertIn("Nothing changed", out)

    def test_rotates_tests_the_login_and_updates_only_that_line(self):
        server = _FakeServer()
        code, out = self.run_script("--yes", server=server)
        self.assertEqual(code, 0, out)
        password = self.new_password(self.env)
        self.assertRegex(password, r"^[A-Za-z0-9]{32}$")
        self.assertEqual(self.env.read_text(encoding="utf-8"), ENV_TEXT.replace("old-secret", password))
        self.assertEqual(server.statements[0], f"ALTER LOGIN [USR_FDE_RO] WITH PASSWORD = '{password}';")
        admin, agent = (c.settings for c in server.connections)
        self.assertTrue(admin.sql_encrypt and agent.sql_encrypt)
        self.assertEqual(agent.sql_admin_user, "USR_FDE_RO")
        self.assertEqual(agent.sql_admin_password.get_secret_value(), password)
        self.assertTrue(server.connections[0].autocommit)  # ALTER LOGIN is not left in an open transaction
        self.assertTrue(all(c.closed for c in server.connections))
        for secret in (password, "old-secret", "admin-secret"):
            self.assertNotIn(secret, out)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertFalse(self.pending.exists())

    def test_failures_leave_env_unchanged_and_keep_a_possibly_live_password(self):
        for kwargs, kept in (({"fail_admin": True}, False), ({"fail_alter": True}, True), ({"fail_agent": True}, True)):
            with self.subTest(**kwargs):
                code, out = self.run_script("--yes", server=_FakeServer(**kwargs))
                self.assertEqual(code, 1)
                self.assertEqual(self.env.read_text(encoding="utf-8"), ENV_TEXT)
                self.assertEqual(self.pending.exists(), kept)
                if kept:
                    self.assertNotIn(self.new_password(self.pending), out)
                    self.assertEqual(stat.S_IMODE(self.pending.stat().st_mode), 0o600)
                    self.pending.unlink()

    def test_unfinished_rotation_blocks_a_new_one(self):
        self.pending.write_text("x", encoding="utf-8")
        server = _FakeServer()
        code, _ = self.run_script("--yes", server=server)
        self.assertEqual((code, server.connections), (1, []))

    def test_bad_configuration_is_rejected_before_connecting(self):
        for text in (
            ENV_TEXT.replace("SQL_ADMIN_PASSWORD=admin-secret\n", ""),
            ENV_TEXT.replace("SQL_AGENT_USER=USR_FDE_RO", "SQL_AGENT_USER=x] WITH"),
            ENV_TEXT.replace("SQL_AGENT_USER=USR_FDE_RO", "SQL_AGENT_USER=SA"),
        ):
            self.env.write_text(text, encoding="utf-8")
            server = _FakeServer()
            self.assertEqual(self.run_script("--yes", server=server)[0], 2)
            self.assertEqual(server.connections, [])

    def test_missing_line_is_appended(self):
        self.assertEqual(rotate.with_new_password("A=1", "pw"), "A=1\nSQL_AGENT_PASSWORD=pw\n")


if __name__ == "__main__":
    unittest.main()
