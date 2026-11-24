"""`/readyz`: 200 only when the telemetry view can be read; same answer from both servers."""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

import api.server as server

SECRET = "PWD=super-secret-1"


class _Cursor:
    def keys(self):
        return ["Timestamp"]

    def fetchmany(self, n):
        return [("2024-08-29",)]


class _Engine:
    def __init__(self, error: Exception | None = None):
        self.error, self.statements = error, []

    def connect(self):
        if self.error:
            raise self.error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.statements.append(str(sql))
        return _Cursor()


def _graph(engine):
    settings = SimpleNamespace(sql_allowed_view="FDE_VIEWS.VW_ACTIVE_FLEET")
    return SimpleNamespace(dispatch_resources=SimpleNamespace(settings=settings, engine=lambda: engine))


def _stdlib_get(path):
    srv = server.make_stdlib_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}{path}", timeout=5) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read())
    finally:
        srv.shutdown()
        srv.server_close()


class TestReadiness(unittest.TestCase):
    def get_both(self, **graph):
        with mock.patch.object(server, "_get_compiled_graph", **graph):
            res = TestClient(server.create_app(agent_streamer=lambda s, p: iter([]))).get("/readyz")
            fastapi = (res.status_code, res.json())
            self.assertEqual(_stdlib_get("/readyz"), fastapi)
        return fastapi

    def test_ready_when_the_view_can_be_read(self):
        engine = _Engine()
        self.assertEqual(self.get_both(return_value=_graph(engine)), (200, {"status": "ready", "telemetry_db": "ok"}))
        self.assertEqual(engine.statements[0], "SELECT TOP 1 * FROM FDE_VIEWS.VW_ACTIVE_FLEET")

    def test_database_failure_is_503_without_details(self):
        status, body = self.get_both(return_value=_graph(_Engine(ValueError(f"login failed {SECRET}"))))
        self.assertEqual((status, body), (503, {"status": "not_ready", "telemetry_db": "ValueError"}))
        self.assertNotIn(SECRET, json.dumps(body))

    def test_agent_build_failure_is_503_without_details(self):
        status, body = self.get_both(side_effect=server.AgentConfigError(f"bad {SECRET}"))
        self.assertEqual((status, body), (503, {"status": "not_ready", "agent": "AgentConfigError"}))

    def test_healthz_stays_up_when_not_ready(self):
        with mock.patch.object(server, "_get_compiled_graph", side_effect=RuntimeError("down")):
            res = TestClient(server.create_app(agent_streamer=lambda s, p: iter([]))).get("/healthz")
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
