"""Shared-secret check for `/api/*`, so a public API only answers the Vercel frontend.

Vercel adds the `X-Origin-Secret` header on the server side (`FE/vercel.json`), so the browser never
holds it. With no secret set the API stays open for local use, except that a request which came
through a reverse proxy is refused: a server that forgot to set the secret must not be public.
"""
from __future__ import annotations

import hmac
import json
import os
from typing import Optional

SECRET_ENV = "API_ORIGIN_SECRET"
SECRET_HEADER = "x-origin-secret"
PROXY_HEADER = "x-forwarded-for"  # Caddy sets this on every request it forwards.
PROTECTED_PREFIX = "/api/"
MIN_SECRET_LENGTH = 32

Rejection = tuple[int, dict[str, str]]


def check_origin(path: str, headers) -> Optional[Rejection]:
    """Returns (status, body) to refuse the request, or None to allow it. `headers` has lower-case or
    case-insensitive keys."""
    if not path.startswith(PROTECTED_PREFIX):
        return None
    expected = os.getenv(SECRET_ENV, "").strip()
    if not expected:
        if headers.get(PROXY_HEADER):
            return 503, {"detail": f"{SECRET_ENV} is not set on this server."}
        return None
    if len(expected) < MIN_SECRET_LENGTH:
        return 503, {"detail": f"{SECRET_ENV} must be at least {MIN_SECRET_LENGTH} characters."}
    supplied = headers.get(SECRET_HEADER) or ""
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return 401, {"detail": "Unauthorized"}
    return None


class OriginSecretMiddleware:
    """Plain ASGI middleware: unlike `@app.middleware("http")` it doesn't wrap the SSE body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            rejection = check_origin(scope["path"], headers)
            if rejection:
                status, body = rejection
                raw = json.dumps(body).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(raw)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": raw})
                return
        await self.app(scope, receive, send)
