"""Pieces shared by the sync and async clients: credentials, request building and response parsing."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any, Protocol, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from ._version import __version__
from .errors import TeleError, error_from_response
from .models import CallUser, InviteOptions, JoinUser
from .tokens import TeleSigner, TeleUser

DEFAULT_BASE_URL = "https://tele-api.cloudguys.io"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = f"tele-fastapi/{__version__}"

M = TypeVar("M", bound=BaseModel)


class Credentials(Protocol):
    def bearer(self) -> str: ...

    @property
    def kind(self) -> str: ...


class StaticToken:
    """A bearer token you minted yourself (or received from somewhere else)."""

    kind = "static"

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("tele: token must not be empty")
        self._token = token

    def bearer(self) -> str:
        return self._token


class _CachedToken:
    """Mints a token and reuses it until it is close to expiry. Thread-safe."""

    kind = "cached"

    def __init__(self, signer: TeleSigner, ttl: float, refresh_before: float) -> None:
        self._signer = signer
        self._ttl = ttl
        self._refresh_before = min(refresh_before, ttl / 2)
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0

    def _mint(self) -> str:
        raise NotImplementedError

    def bearer(self) -> str:
        with self._lock:
            now = self._signer.now()
            if not self._token or now >= self._expires - self._refresh_before:
                self._token = self._mint()
                self._expires = now + self._ttl
            return self._token


class ServiceTokens(_CachedToken):
    kind = "service"

    def _mint(self) -> str:
        return self._signer.service_token(self._ttl)


class UserTokens(_CachedToken):
    kind = "user"

    def __init__(self, signer: TeleSigner, user: TeleUser, ttl: float, refresh_before: float) -> None:
        super().__init__(signer, ttl, refresh_before)
        self.user = user
        signer.user_token(user, ttl)  # validate the user up front

    def _mint(self) -> str:
        return self._signer.user_token(self.user, self._ttl)


def ttl_seconds(ttl: float | timedelta) -> float:
    return ttl.total_seconds() if isinstance(ttl, timedelta) else float(ttl)


def seg(value: str) -> str:
    """Escape one path segment."""
    if not value:
        raise ValueError("tele: path parameters must not be empty")
    return quote(value, safe="")


def ts(value: datetime) -> str:
    """RFC 3339 for a timezone-aware datetime."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("tele: datetimes must be timezone-aware (for example datetime.now(timezone.utc))")
    return value.isoformat()


def day_or_ts(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return ts(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def params(**values: Any) -> dict[str, str]:
    """Query parameters, dropping ``None``."""
    out: dict[str, str] = {}
    for k, v in values.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, (datetime, date)):
            out[k] = day_or_ts(v)
        else:
            out[k] = str(v)
    return out


def body(**values: Any) -> dict[str, Any]:
    """A JSON body, dropping ``None`` (Tele treats a missing field as "unchanged" or "default")."""
    out: dict[str, Any] = {}
    for k, v in values.items():
        if v is None:
            continue
        out[k] = ts(v) if isinstance(v, datetime) else v
    return out


def user_input(u: CallUser | Mapping[str, Any]) -> dict[str, Any]:
    return CallUser.model_validate(u).model_dump(mode="json", exclude_none=True)


def join_user_input(u: JoinUser | Mapping[str, Any]) -> dict[str, Any]:
    return JoinUser.model_validate(u).model_dump(mode="json", exclude_none=True)


def invite_input(i: InviteOptions | Mapping[str, Any]) -> dict[str, Any]:
    opts = InviteOptions.model_validate(i)
    return body(label=opts.label, expires_at=opts.expires_at, max_uses=opts.max_uses)


def join_headers(platform: str | None, forwarded_for: str | None, user_agent: str | None) -> dict[str, str]:
    h: dict[str, str] = {}
    if platform:
        h["X-Client-Platform"] = platform.strip().lower()[:32]
    if forwarded_for:
        h["X-Forwarded-For"] = forwarded_for
    if user_agent:
        h["User-Agent"] = user_agent[:512]
    return h


def check(response: httpx.Response) -> httpx.Response:
    if response.is_success:
        return response
    raise error_from_response(response)


def model(cls: type[M], response: httpx.Response) -> M:
    return cls.model_validate(response.json())


def models(cls: type[M], response: httpx.Response, key: str) -> list[M]:
    return [cls.model_validate(item) for item in response.json()[key]]


def build_headers(credentials: Credentials | None, extra: Mapping[str, str]) -> dict[str, str]:
    h = {"Accept": "application/json"}
    h.update(extra)
    if credentials is not None:
        h["Authorization"] = f"Bearer {credentials.bearer()}"
    return h


class RecordingStreamBase:
    """Shared accessors for a streamed recording response."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    @property
    def status_code(self) -> int:
        return self.response.status_code

    @property
    def partial(self) -> bool:
        """True for a ``206 Partial Content`` answer to a ``Range`` request."""
        return self.response.status_code == 206

    @property
    def headers(self) -> httpx.Headers:
        return self.response.headers

    @property
    def content_type(self) -> str:
        return str(self.response.headers.get("content-type", "video/mp4"))

    @property
    def content_length(self) -> int | None:
        v = self.response.headers.get("content-length")
        return int(v) if v is not None else None

    @property
    def content_range(self) -> str | None:
        return self._header("content-range")

    @property
    def etag(self) -> str | None:
        return self._header("etag")

    def _header(self, name: str) -> str | None:
        v = self.response.headers.get(name)
        return str(v) if v is not None else None

    def passthrough_headers(self) -> dict[str, str]:
        """The headers worth relaying when you proxy the video to a browser."""
        keep = ("content-type", "content-length", "content-range", "accept-ranges", "etag")
        out = {k: v for k, v in self.response.headers.items() if k.lower() in keep}
        out["Cache-Control"] = "private, no-store"
        return out


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "USER_AGENT",
    "Credentials",
    "RecordingStreamBase",
    "ServiceTokens",
    "StaticToken",
    "TeleError",
    "UserTokens",
]
