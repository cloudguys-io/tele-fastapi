"""Sign Tele API tokens on your backend.

Tele authenticates every request with an HS256 JWT signed with your API secret. User tokens act as one of your
users (``sub``) and may live at most one hour; service tokens act as your backend and may live at most ten
minutes. The API secret must never leave your backend: hand user tokens to your apps instead.

This is a port of ``teletoken`` (Go) and ``@tele/node``: same claims, same limits, same validation.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import jwt

__all__ = [
    "MAX_SERVICE_TTL_SECONDS",
    "MAX_USER_TTL_SECONDS",
    "TeleSigner",
    "TeleUser",
    "TokenError",
]

MAX_USER_TTL_SECONDS = 3600
MAX_SERVICE_TTL_SECONDS = 600

Scope = Literal["user", "service"]


class TokenError(ValueError):
    """A token could not be signed because an argument is invalid."""


@dataclass(frozen=True)
class TeleUser:
    """One of your end users.

    ``id`` becomes the token's ``sub`` (required, at most 128 characters). ``name`` is the display name shown to
    other participants and ``team`` is used for billing attribution (each at most 64 characters).
    """

    id: str
    name: str = ""
    team: str = ""


def _seconds(ttl: float | timedelta) -> float:
    return ttl.total_seconds() if isinstance(ttl, timedelta) else float(ttl)


class TeleSigner:
    """Signs user and service tokens with your API key and secret.

    >>> signer = TeleSigner(api_key="TK...", api_secret="...")
    >>> signer.user_token(TeleUser(id="user-123", name="Rahim", team="sales"), ttl=900)
    """

    def __init__(self, api_key: str, api_secret: str, *, now: Callable[[], float] | None = None) -> None:
        if not api_key or not api_secret:
            raise TokenError("tele: api_key and api_secret are required")
        self.api_key = api_key
        self._api_secret = api_secret
        self._now = now or time.time

    def __repr__(self) -> str:  # never show the secret
        return f"TeleSigner(api_key={self.api_key!r})"

    def now(self) -> int:
        """The signer's clock in whole Unix seconds."""
        return int(self._now())

    def user_token(self, user: TeleUser, ttl: float | timedelta = 900) -> str:
        """A token your app uses to act as ``user``: create calls it hosts, preview invites, join. ttl ≤ 3600 s."""
        if not user.id:
            raise TokenError("tele: user.id is required")
        if len(user.id) > 128:
            raise TokenError("tele: user.id longer than 128 characters")
        if len(user.name) > 64 or len(user.team) > 64:
            raise TokenError("tele: name and team are limited to 64 characters")
        extra: dict[str, Any] = {"sub": user.id}
        if user.name:
            extra["name"] = user.name
        if user.team:
            extra["team"] = user.team
        return self._sign("user", _seconds(ttl), MAX_USER_TTL_SECONDS, extra)

    def service_token(self, ttl: float | timedelta = 300) -> str:
        """A token for server-to-server calls from your backend. ttl ≤ 600 s. Never send it to an app."""
        return self._sign("service", _seconds(ttl), MAX_SERVICE_TTL_SECONDS, {})

    def _sign(self, scope: Scope, ttl: float, max_ttl: int, extra: dict[str, Any]) -> str:
        if not (0 < ttl <= max_ttl):
            raise TokenError(f"tele: {scope} token ttl must be in (0, {max_ttl}]")
        iat = self.now()
        payload: dict[str, Any] = {"iss": self.api_key, "iat": iat, "nbf": iat, "exp": iat + int(ttl)}
        payload.update(extra)
        payload["tele"] = {"scope": scope}
        return jwt.encode(payload, self._api_secret, algorithm="HS256")
