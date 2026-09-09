from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException


_DISPLAY_NAMES = {"deezer": "Deezer", "soundcloud": "SoundCloud"}


@dataclass(frozen=True)
class ProviderErrorSpec:
    code: str
    service: str
    message: str
    retryable: bool
    status_code: int

    def detail(self) -> dict:
        return {
            "code": self.code,
            "service": self.service,
            "message": self.message,
            "retryable": self.retryable,
        }

    def http_exception(self) -> HTTPException:
        return HTTPException(status_code=self.status_code, detail=self.detail())


def provider_error(
    service: str,
    code: str,
    message: str,
    *,
    retryable: bool,
    status_code: int,
) -> HTTPException:
    return ProviderErrorSpec(code, service, message, retryable, status_code).http_exception()


def provider_collection_incomplete(service: str) -> HTTPException:
    name = _DISPLAY_NAMES.get(service, service.title())
    return provider_error(
        service,
        "provider_collection_incomplete",
        f"{name} returned an incomplete collection",
        retryable=True,
        status_code=502,
    )


def provider_pagination_invalid(service: str) -> HTTPException:
    name = _DISPLAY_NAMES.get(service, service.title())
    return provider_error(
        service,
        "provider_pagination_invalid",
        f"{name} returned an invalid pagination link",
        retryable=False,
        status_code=502,
    )


def _status_from_exception(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def provider_exception(service: str, exc: BaseException) -> HTTPException:
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        return exc
    name = _DISPLAY_NAMES.get(service, service.title())
    status = _status_from_exception(exc)
    if status == 401 or exc.__class__.__name__ == "GraphQLClientAuthError":
        return provider_error(
            service,
            "provider_auth_required",
            f"{name} login is required",
            retryable=False,
            status_code=401,
        )
    if status == 403:
        return provider_error(
            service,
            "provider_access_denied",
            f"{name} did not allow this operation",
            retryable=False,
            status_code=403,
        )
    if status == 429:
        return provider_error(
            service,
            "provider_rate_limited",
            f"{name} rate limit was reached",
            retryable=True,
            status_code=429,
        )
    if status is not None and status >= 500:
        return provider_error(
            service,
            "provider_unavailable",
            f"{name} is temporarily unavailable",
            retryable=True,
            status_code=503,
        )
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return provider_error(
            service,
            "provider_unavailable",
            f"{name} is temporarily unavailable",
            retryable=True,
            status_code=503,
        )
    return provider_error(
        service,
        "provider_request_failed",
        f"{name} operation failed",
        retryable=True,
        status_code=502,
    )


def detail_from_exception(service: str, exc: BaseException) -> dict:
    return dict(provider_exception(service, exc).detail)
