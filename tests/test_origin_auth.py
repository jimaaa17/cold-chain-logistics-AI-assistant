"""Shared-secret guard on `/api/*` and the audit sign-in switch; same answers from both servers."""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import api.server as server
from api.origin_auth import check_origin

SECRET = "s" * 40
PROXIED = {"X-Forwarded-For": "203.0.113.9"}
FE_ROOT = Path(__file__).resolve().parents[1] / "FE"


def _events(session_id, prompt):
    yield {"type": "final_answer", "content": "ok", "node": "reasoner_final"}


def _stdlib(method, path, headers, body=None):
    srv = server.make_stdlib_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.server_address[1]}{path}", data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read()), res.headers
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read()), exc.headers
    finally:
        srv.shutdown()
        srv.server_close()


class OriginCase(unittest.TestCase):
    def setUp(self):
        self.audit_calls = []
        self.client = TestClient(
            server.create_app(
                agent_streamer=_events,
                audit_writer=lambda *a: None,
                audit_reader=lambda **kw: self.audit_calls.append(kw) or [],
            )
        )

    def env(self, **values):
        base = {"API_ORIGIN_SECRET": "", "AUDIT_LOGIN_ENABLED": ""}
        return mock.patch.dict("os.environ", {**base, **values})

    def both(self, method, path, headers=None, body=None):
        """(status, body) from FastAPI, after checking the stdlib server agrees."""
        headers = headers or {}
        res = self.client.request(method, path, headers=headers, json=body)
        status, payload, _ = _stdlib(method, path, {**headers, "Content-Type": "application/json"}, body)
        self.assertEqual((status, payload), (res.status_code, res.json()))
        return res.status_code, res.json()


class TestOriginSecret(OriginCase):
    def test_open_locally_when_no_secret_is_set(self):
        with self.env():
            self.assertEqual(self.client.get("/api/v1/meta").status_code, 200)

    def test_proxied_request_is_refused_when_no_secret_is_set(self):
        with self.env():
            status, body = self.both("GET", "/api/v1/meta", PROXIED)
        self.assertEqual(status, 503)
        self.assertIn("API_ORIGIN_SECRET", body["detail"])

    def test_missing_wrong_and_unexpanded_secrets_are_401(self):
        with self.env(API_ORIGIN_SECRET=SECRET):
            for value in (None, "", "wrong", SECRET + "x", SECRET[:-1], "$ORIGIN_SECRET"):
                headers = {} if value is None else {"X-Origin-Secret": value}
                with self.subTest(value=value):
                    self.assertEqual(self.both("GET", "/api/v1/meta", headers), (401, {"detail": "Unauthorized"}))

    def test_non_ascii_secret_is_401_not_an_error(self):
        with self.env(API_ORIGIN_SECRET=SECRET):
            res = self.client.get("/api/v1/meta", headers={"X-Origin-Secret": "é".encode("latin-1")})
        self.assertEqual(res.status_code, 401)

    def test_short_secret_fails_closed_even_when_it_matches(self):
        with self.env(API_ORIGIN_SECRET="short"):
            self.assertEqual(self.both("GET", "/api/v1/meta", {"X-Origin-Secret": "short"})[0], 503)

    def test_every_api_route_is_protected(self):
        routes = [
            (method, route.path)
            for route in self.client.app.routes
            if getattr(route, "path", "").startswith("/api/")
            for method in route.methods - {"HEAD", "OPTIONS"}
        ]
        self.assertGreaterEqual(len(routes), 6)
        with self.env(API_ORIGIN_SECRET=SECRET):
            for method, path in routes:
                with self.subTest(route=f"{method} {path}"):
                    self.assertEqual(self.both(method, path, body={} if method == "POST" else None)[0], 401)

    def test_encoded_path_does_not_bypass_the_check(self):
        with self.env(API_ORIGIN_SECRET=SECRET):
            self.assertEqual(self.client.get("/%61pi/v1/meta").status_code, 401)

    def test_right_secret_streams_a_dispatch(self):
        with self.env(API_ORIGIN_SECRET=SECRET):
            res = self.client.post(
                "/api/v1/dispatch/stream",
                headers={"X-Origin-Secret": SECRET, **PROXIED},
                json={"session_id": "s1", "prompt": "status?"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["cache-control"], "no-cache")
        self.assertIn("event: final_answer", res.text)

    def test_health_checks_stay_open_for_the_deploy(self):
        with self.env(API_ORIGIN_SECRET=SECRET):
            self.assertEqual(self.client.get("/healthz").status_code, 200)
            self.assertIsNone(check_origin("/readyz", {}))

    def test_no_cors_headers(self):
        with self.env():
            res = self.client.get("/api/v1/meta", headers={"Origin": "https://evil.example"})
            _, _, stdlib_headers = _stdlib("GET", "/api/v1/meta", {"Origin": "https://evil.example"})
        self.assertNotIn("access-control-allow-origin", res.headers)
        self.assertIsNone(stdlib_headers["Access-Control-Allow-Origin"])


class TestAuditLoginSwitch(OriginCase):
    BODY = {"username": "sa", "password": "guess"}

    def test_disabled_is_403_and_never_reaches_sql(self):
        with self.env(AUDIT_LOGIN_ENABLED="false"), mock.patch.object(server, "default_audit_reader") as reader:
            status, body = self.both("POST", "/api/v1/audit/logs", body=self.BODY)
        self.assertEqual(status, 403)
        self.assertIn("turned off", body["detail"])
        self.assertEqual(self.audit_calls, [])
        reader.assert_not_called()

    def test_enabled_by_default(self):
        with self.env():
            self.assertEqual(self.client.post("/api/v1/audit/logs", json=self.BODY).status_code, 200)
        self.assertEqual(len(self.audit_calls), 1)


class TestVercelConfig(unittest.TestCase):
    def test_api_route_adds_the_secret_from_the_environment_over_https(self):
        config = json.loads((FE_ROOT / "vercel.json").read_text())
        route = next(r for r in config["routes"] if r.get("src", "").startswith("/api/"))
        self.assertTrue(route["dest"].startswith("https://"))
        self.assertTrue(route["dest"].endswith("/api/$1"))
        transform = route["transforms"][0]
        self.assertEqual((transform["type"], transform["op"]), ("request.headers", "set"))
        self.assertEqual(transform["target"]["key"], "x-origin-secret")
        self.assertEqual((transform["args"], transform["env"]), ("$ORIGIN_SECRET", ["ORIGIN_SECRET"]))


if __name__ == "__main__":
    unittest.main()
