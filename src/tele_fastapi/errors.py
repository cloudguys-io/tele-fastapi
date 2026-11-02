"""Errors raised by the Tele clients.

Every Tele error has the same body: ``{"error": {"code": "...", "message": "...", "opens_at": "..."}}``.
Branch on :attr:`TeleError.code`; ``message`` is for logs and may change.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

__all__ = [
    "AuthenticationError",
    "BadRequestError",
    "ConflictError",
    "GoneError",
    "NotFoundError",
    "NotImplementedByServerError",
    "PermissionDeniedError",
    "RateLimitedError",
    "ServerError",
    "TeleError",
]


class TeleError(Exception):
    """An error response from the Tele API."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        opens_at: datetime | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.opens_at = opens_at
        self.body = body

    @property
    def retryable(self) -> bool:
        """``5xx`` and ``429`` are safe to retry with backoff (but see the guide about joins and creating calls)."""
        return self.status == 429 or self.status >= 500

    @property
    def is_join_window(self) -> bool:
        """The call is not open yet (``too_early``) or has closed (``too_late``)."""
        return self.code in ("too_early", "too_late")

    @property
    def is_invite_invalid(self) -> bool:
        """The invite link no longer works: unknown, expired, revoked or used up."""
        return self.code in ("invite_not_found", "invite_expired", "invite_revoked", "invite_exhausted")

    @property
    def is_call_closed(self) -> bool:
        """The call has ended or was cancelled."""
        return self.code in ("call_ended", "call_cancelled", "call_closed")


class BadRequestError(TeleError):
    """400: ``invalid_request`` or ``invalid_body``."""


class AuthenticationError(TeleError):
    """401 ``unauthenticated``: get a fresh token, check the key and secret."""


class PermissionDeniedError(TeleError):
    """403: ``forbidden`` or ``not_invited``."""


class NotFoundError(TeleError):
    """404: ``not_found`` or ``invite_not_found``."""


class ConflictError(TeleError):
    """409: join window, call state, recording state and similar conflicts. See ``code`` and ``opens_at``."""


class GoneError(TeleError):
    """410: ``invite_expired``, ``invite_revoked``, ``invite_exhausted`` or ``expired`` (recordings)."""


class RateLimitedError(TeleError):
    """429 ``rate_limited``: back off."""


class NotImplementedByServerError(TeleError):
    """501 ``recording_disabled``: the feature is not enabled on this server."""


class ServerError(TeleError):
    """5xx: something went wrong on Tele's side. Retry with backoff."""


_BY_STATUS: dict[int, type[TeleError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    410: GoneError,
    429: RateLimitedError,
    501: NotImplementedByServerError,
}


_DATETIME = TypeAdapter(datetime)


def _parse_time(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return _DATETIME.validate_python(v)
    except ValidationError:
        return None


def error_from_parts(status: int, content: bytes, headers: httpx.Headers) -> TeleError:
    """Build the right :class:`TeleError` subclass from a response's status, body and headers."""
    body: Any = None
    code, message, opens_at = f"http_{status}", "", None
    try:
        body = json.loads(content) if content else None
    except ValueError:
        message = content[:200].decode(errors="replace")
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        code = str(err.get("code") or code)
        message = str(err.get("message") or "")
        opens_at = _parse_time(err.get("opens_at"))
    cls = _BY_STATUS.get(status) or (ServerError if status >= 500 else TeleError)
    return cls(
        status,
        code,
        message or httpx.codes.get_reason_phrase(status),
        request_id=headers.get("x-request-id"),
        opens_at=opens_at,
        body=body,
    )


def error_from_response(response: httpx.Response) -> TeleError:
    """Build a :class:`TeleError` from a (read) non-2xx response."""
    return error_from_parts(response.status_code, response.content, response.headers)
