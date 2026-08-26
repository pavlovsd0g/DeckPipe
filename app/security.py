from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit


PUBLIC_PATHS = {"/", "/api/version"}
PUBLIC_PREFIXES = ("/static/",)
ALLOWED_ORIGIN = "http://tauri.localhost"
ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
ALLOWED_HEADERS = ("Authorization", "Content-Type")
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@dataclass(frozen=True)
class SecuritySettings:
    bound_host: str
    bound_port: int
    api_token: str
    allowed_origin: str = ALLOWED_ORIGIN

    @classmethod
    def from_env(cls) -> "SecuritySettings":
        token = os.environ.get("DECKPIPE_API_TOKEN")
        if not token:
            raise RuntimeError("DECKPIPE_API_TOKEN is required")
        host = os.environ.get("DECKPIPE_BOUND_HOST", "127.0.0.1")
        port_value = os.environ.get("DECKPIPE_BOUND_PORT") or os.environ.get("DECKPIPE_PORT")
        if not port_value:
            raise RuntimeError("DECKPIPE_BOUND_PORT is required")
        try:
            port = int(port_value)
        except ValueError as exc:
            raise ValueError("DECKPIPE_BOUND_PORT must be an integer") from exc
        return cls.for_launch(host, port, token)

    @classmethod
    def for_launch(cls, host: str, port: int, token: str) -> "SecuritySettings":
        normalized_host = _normalize_bind_host(host)
        if not token:
            raise RuntimeError("DECKPIPE_API_TOKEN is required")
        if not (1 <= int(port) <= 65535):
            raise ValueError("bound port must be between 1 and 65535")
        return cls(bound_host=normalized_host, bound_port=int(port), api_token=token)


class LoopbackSecurityMiddleware:
    def __init__(self, app, settings: SecuritySettings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_headers = _headers(scope.get("headers", []))
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        origin = request_headers.get("origin")

        host_error_status = self._host_error_status(scope.get("headers", []))
        if host_error_status is not None:
            await self._json(send, host_error_status, "Bad request", origin)
            return

        if self._has_hostile_browser_metadata(request_headers):
            await self._json(send, 403, "Forbidden", origin)
            return

        if method == "OPTIONS" and "access-control-request-method" in request_headers:
            if not self._is_allowed_preflight(request_headers):
                await self._json(send, 403, "Forbidden", origin)
                return
            await self._empty_preflight(send)
            return

        if self._requires_auth(path) and not self._authorized(request_headers):
            await self._json(send, 401, "Unauthorized", origin)
            return

        async def secure_send(message):
            if message.get("type") == "http.response.start":
                message["headers"] = self._secure_headers(message.get("headers", []), origin)
            await send(message)

        await self.app(scope, receive, secure_send)

    def _host_error_status(self, raw_headers: Iterable[tuple[bytes, bytes]]) -> int | None:
        host_values = [
            value.decode("latin-1")
            for key, value in raw_headers
            if key.lower() == b"host"
        ]
        if len(host_values) != 1:
            return 400
        parsed = _parse_host(host_values[0])
        if parsed is None:
            return 400
        host, port = parsed
        if host not in LOOPBACK_HOSTS or port != self.settings.bound_port:
            return 403
        return None

    def _has_hostile_browser_metadata(self, headers: dict[str, str]) -> bool:
        origin = headers.get("origin")
        if origin is not None and origin != self.settings.allowed_origin:
            return True

        referer = headers.get("referer")
        if referer:
            try:
                referer_origin = _origin_from_url(referer)
            except ValueError:
                return True
            if referer_origin not in {self.settings.allowed_origin, _bound_origin(self.settings)}:
                return True

        fetch_site = headers.get("sec-fetch-site")
        if fetch_site and fetch_site.lower() == "cross-site":
            return True

        return False

    def _is_allowed_preflight(self, headers: dict[str, str]) -> bool:
        if headers.get("origin") != self.settings.allowed_origin:
            return False
        requested_method = headers.get("access-control-request-method", "").upper()
        if requested_method not in ALLOWED_METHODS:
            return False
        requested_headers = {
            value.strip().lower()
            for value in headers.get("access-control-request-headers", "").split(",")
            if value.strip()
        }
        allowed_headers = {value.lower() for value in ALLOWED_HEADERS}
        return requested_headers.issubset(allowed_headers)

    def _requires_auth(self, path: str) -> bool:
        return path.startswith("/api/") and not _is_public_path(path)

    def _authorized(self, headers: dict[str, str]) -> bool:
        prefix = "Bearer "
        value = headers.get("authorization", "")
        if not value.startswith(prefix):
            return False
        supplied = value[len(prefix):]
        if not supplied:
            return False
        return hmac.compare_digest(supplied, self.settings.api_token)

    async def _json(self, send, status: int, message: str, origin: str | None) -> None:
        body = json.dumps({"detail": message}, separators=(",", ":")).encode("utf-8")
        headers = self._secure_headers(
            [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
            origin,
        )
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _empty_preflight(self, send) -> None:
        headers = self._secure_headers(
            [
                (b"content-length", b"0"),
                (b"access-control-allow-origin", self.settings.allowed_origin.encode("ascii")),
                (b"access-control-allow-methods", b", ".join(m.encode("ascii") for m in ALLOWED_METHODS)),
                (b"access-control-allow-headers", b", ".join(h.encode("ascii") for h in ALLOWED_HEADERS)),
                (b"vary", b"Origin"),
            ],
            self.settings.allowed_origin,
        )
        await send({"type": "http.response.start", "status": 204, "headers": headers})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    def _secure_headers(
        self, headers: Iterable[tuple[bytes, bytes]], origin: str | None
    ) -> list[tuple[bytes, bytes]]:
        result = list(headers)
        existing = {key.lower() for key, _value in result}
        additions = {
            b"x-content-type-options": b"nosniff",
            b"referrer-policy": b"no-referrer",
            b"x-frame-options": b"DENY",
            b"cross-origin-resource-policy": b"same-origin",
            b"cache-control": b"no-store",
            b"permissions-policy": b"geolocation=(), microphone=(), camera=()",
        }
        for key, value in additions.items():
            if key not in existing:
                result.append((key, value))
        if origin == self.settings.allowed_origin and b"access-control-allow-origin" not in existing:
            result.append((b"access-control-allow-origin", self.settings.allowed_origin.encode("ascii")))
            if b"vary" not in existing:
                result.append((b"vary", b"Origin"))
        return result


def _headers(raw_headers: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in raw_headers
    }


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _normalize_bind_host(host: str) -> str:
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("backend host must be loopback: 127.0.0.1 or ::1")
    return host


def _parse_host(value: str) -> tuple[str, int] | None:
    if not value or any(char in value for char in "/\\@"):
        return None
    if value.startswith("["):
        end = value.find("]")
        if end <= 1:
            return None
        host = value[1:end].lower()
        remainder = value[end + 1:]
        if not remainder.startswith(":"):
            return None
        port_value = remainder[1:]
    else:
        if value.count(":") != 1:
            return None
        host, port_value = value.rsplit(":", 1)
        host = host.lower()
    if not host or not port_value or not port_value.isdecimal():
        return None
    try:
        port = int(port_value)
    except ValueError:
        return None
    return host, port


def _origin_from_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid browser metadata")
    port = parsed.port
    if port is None:
        return f"{parsed.scheme}://{parsed.hostname}"
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


def _bound_origin(settings: SecuritySettings) -> str:
    host = f"[{settings.bound_host}]" if settings.bound_host == "::1" else settings.bound_host
    return f"http://{host}:{settings.bound_port}"
