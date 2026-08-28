from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SENTINEL_TOKEN = "sentinel-launch-token-DO-NOT-LEAK"
BOUND_PORT = 7100


@contextmanager
def configured_environment(token: str = SENTINEL_TOKEN):
    updates = {
        "DECKPIPE_API_TOKEN": token,
        "DECKPIPE_BOUND_HOST": "127.0.0.1",
        "DECKPIPE_BOUND_PORT": str(BOUND_PORT),
    }
    with patch.dict(os.environ, updates, clear=False):
        yield


def fresh_app():
    with configured_environment():
        sys.modules.pop("app.main", None)
        module = importlib.import_module("app.main")
        return module.app


async def asgi_request(
    app,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
):
    sent_request = False
    events: list[dict[str, object]] = []
    raw_headers = [
        (key.lower().encode("ascii"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 53000),
        "server": ("127.0.0.1", BOUND_PORT),
    }

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        events.append(message)

    await app(scope, receive, send)
    start = next(event for event in events if event["type"] == "http.response.start")
    response_body = b"".join(
        event.get("body", b"")
        for event in events
        if event["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }
    return int(start["status"]), response_headers, response_body


def request(method: str, path: str, headers: dict[str, str] | None = None):
    default_headers = {"host": f"127.0.0.1:{BOUND_PORT}"}
    default_headers.update(headers or {})
    return asyncio.run(asgi_request(fresh_app(), method, path, default_headers))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SecurityContractTests(unittest.TestCase):
    def assert_generic_unauthorized(self, responses: Iterable[tuple[int, dict, bytes]]) -> None:
        bodies = []
        for status, _headers, body in responses:
            self.assertEqual(401, status)
            self.assertNotIn(SENTINEL_TOKEN.encode("utf-8"), body)
            bodies.append(body)
        self.assertEqual(1, len(set(bodies)))

    def test_launch_settings_require_secret_and_loopback_bind(self) -> None:
        from app.security import SecuritySettings

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DECKPIPE_API_TOKEN", None)
            with self.assertRaisesRegex(RuntimeError, "DECKPIPE_API_TOKEN"):
                SecuritySettings.from_env()

        with self.assertRaisesRegex(ValueError, "loopback"):
            SecuritySettings.for_launch("0.0.0.0", BOUND_PORT, SENTINEL_TOKEN)

        for host in ("127.0.0.1", "::1"):
            settings = SecuritySettings.for_launch(host, BOUND_PORT, SENTINEL_TOKEN)
            self.assertEqual(host, settings.bound_host)

    def test_public_routes_do_not_require_authorization_or_disclose_token(self) -> None:
        for path in ("/", "/static/index.html", "/api/version"):
            status, _headers, body = request("GET", path)
            self.assertEqual(200, status, path)
            self.assertNotIn(SENTINEL_TOKEN.encode("utf-8"), body)
        status, _headers, body = request("GET", "/api/version")
        self.assertEqual(
            {"version": "0.6.0", "build_id": "0.6.0+20260827.050713.6456dba254a6"},
            json.loads(body.decode("utf-8")),
        )

    def test_framework_documentation_surfaces_are_disabled(self) -> None:
        for path in ("/docs", "/redoc", "/openapi.json"):
            status, _headers, body = request("GET", path)
            self.assertEqual(404, status, path)
            self.assertNotIn(SENTINEL_TOKEN.encode("utf-8"), body)

    def test_sensitive_api_routes_require_exact_bearer_token(self) -> None:
        unauthenticated = request("GET", "/api/jobs")
        malformed = request("GET", "/api/jobs", {"authorization": SENTINEL_TOKEN})
        wrong = request("GET", "/api/jobs", {"authorization": "Bearer wrong"})

        self.assert_generic_unauthorized([unauthenticated, malformed, wrong])

        status, _headers, body = request(
            "GET", "/api/jobs", {"authorization": f"Bearer {SENTINEL_TOKEN}"}
        )
        self.assertEqual(200, status)
        self.assertEqual([], json.loads(body.decode("utf-8")))

    def test_allowed_origin_does_not_bypass_missing_or_wrong_bearer_token(self) -> None:
        allowed_origin = {"origin": "http://tauri.localhost"}
        missing = request("GET", "/api/jobs", allowed_origin)
        wrong = request(
            "GET",
            "/api/jobs",
            {**allowed_origin, "authorization": "Bearer wrong"},
        )

        self.assert_generic_unauthorized([missing, wrong])
        for _status, headers, _body in (missing, wrong):
            self.assertEqual("http://tauri.localhost", headers.get("access-control-allow-origin"))
            self.assertNotIn("access-control-allow-credentials", headers)

    def test_cors_preflight_is_exact_and_not_a_wildcard_bypass(self) -> None:
        status, headers, body = request(
            "OPTIONS",
            "/api/jobs",
            {
                "origin": "http://tauri.localhost",
                "access-control-request-method": "GET",
                "access-control-request-headers": "Authorization, Content-Type",
            },
        )

        self.assertEqual(204, status)
        self.assertEqual(b"", body)
        self.assertEqual("http://tauri.localhost", headers.get("access-control-allow-origin"))
        self.assertNotEqual("*", headers.get("access-control-allow-origin"))
        self.assertNotIn("access-control-allow-credentials", headers)
        self.assertNotIn("access-control-allow-private-network", headers)
        allowed_methods = {
            method.strip()
            for method in headers.get("access-control-allow-methods", "").split(",")
            if method.strip()
        }
        self.assertEqual({"GET", "POST", "DELETE", "OPTIONS"}, allowed_methods)
        self.assertIn("Authorization", headers.get("access-control-allow-headers", ""))

    def test_packaged_origin_may_cross_site_fetch_the_loopback_api(self) -> None:
        browser_metadata = {
            "origin": "http://tauri.localhost",
            "sec-fetch-site": "cross-site",
        }
        preflight = request(
            "OPTIONS",
            "/api/jobs",
            {
                **browser_metadata,
                "access-control-request-method": "GET",
                "access-control-request-headers": "Authorization",
            },
        )
        authorized = request(
            "GET",
            "/api/jobs",
            {
                **browser_metadata,
                "authorization": f"Bearer {SENTINEL_TOKEN}",
            },
        )

        self.assertEqual(204, preflight[0])
        self.assertEqual(200, authorized[0])
        for _status, headers, body in (preflight, authorized):
            self.assertEqual(
                "http://tauri.localhost",
                headers.get("access-control-allow-origin"),
            )
            self.assertNotIn(SENTINEL_TOKEN.encode("utf-8"), body)

    def test_hostile_browser_requests_are_rejected_before_auth_or_routing(self) -> None:
        hostile_get = request(
            "GET",
            "/api/jobs",
            {
                "origin": "https://evil.example",
                "authorization": f"Bearer {SENTINEL_TOKEN}",
            },
        )
        hostile_preflight = request(
            "OPTIONS",
            "/api/jobs",
            {
                "origin": "https://evil.example",
                "access-control-request-method": "GET",
                "access-control-request-headers": "Authorization",
            },
        )

        for status, headers, body in (hostile_get, hostile_preflight):
            self.assertEqual(403, status)
            self.assertNotEqual("*", headers.get("access-control-allow-origin"))
            self.assertNotIn(SENTINEL_TOKEN.encode("utf-8"), body)

    def test_correct_token_cannot_override_hostile_request_metadata(self) -> None:
        auth = {"authorization": f"Bearer {SENTINEL_TOKEN}"}
        hostile_cases = (
            {"host": f"evil.example:{BOUND_PORT}"},
            {"host": "127.0.0.1:9999"},
            {"host": "http://127.0.0.1:7100"},
            {"origin": "https://evil.example"},
            {"referer": "https://evil.example/path"},
            {"sec-fetch-site": "cross-site"},
        )

        for headers in hostile_cases:
            status, _response_headers, body = request("GET", "/api/jobs", {**auth, **headers})
            self.assertIn(status, {400, 403})
            self.assertNotIn(SENTINEL_TOKEN.encode("utf-8"), body)

    def test_response_headers_are_restrictive_for_json_api(self) -> None:
        status, headers, _body = request(
            "GET", "/api/jobs", {"authorization": f"Bearer {SENTINEL_TOKEN}"}
        )

        self.assertEqual(200, status)
        self.assertEqual("nosniff", headers.get("x-content-type-options"))
        self.assertEqual("no-referrer", headers.get("referrer-policy"))
        self.assertEqual("DENY", headers.get("x-frame-options"))
        self.assertNotIn("content-security-policy", headers)

    def test_deezer_login_contract_exposes_only_arl_and_no_password_auth_surface(self) -> None:
        sources = {
            "app/deezer_client.py": read_text(ROOT / "app" / "deezer_client.py"),
            "app/main.py": read_text(ROOT / "app" / "main.py"),
            "frontend/app.js": read_text(ROOT / "frontend" / "app.js"),
            "frontend/index.html": read_text(ROOT / "frontend" / "index.html"),
            "app/static/app.js": read_text(ROOT / "app" / "static" / "app.js"),
            "app/static/index.html": read_text(ROOT / "app" / "static" / "index.html"),
            "desktop/ui/app.js": read_text(ROOT / "desktop" / "ui" / "app.js"),
            "desktop/ui/index.html": read_text(ROOT / "desktop" / "ui" / "index.html"),
        }
        combined = "\n".join(sources.values())

        for forbidden in [
            "DZ_CLIENT_SECRET",
            "DZ_CLIENT_ID",
            "login_with_password",
            "/api/login/deezer/password",
            "user_auth.php",
            "loginPassword",
            "loginEmail",
            "loginPasswordBlock",
        ]:
            self.assertNotIn(forbidden, combined)
        self.assertNotRegex(combined, r"type=[\"']password[\"']")
        self.assertNotRegex(sources["app/deezer_client.py"], r"client[_-]?secret\s*=", re.IGNORECASE)
        self.assertIn("/api/login/deezer", combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
