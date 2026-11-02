"""FastAPI integration: a token endpoint, a join proxy, webhook verification and a shared client.

Needs the ``fastapi`` extra: ``pip install "tele-fastapi[fastapi]"``.
"""

# No ``from __future__ import annotations`` here: FastAPI before 0.124 can't resolve string annotations on a
# dependency class's ``__call__``, so these annotations must be real objects at runtime.

import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
import jwt

try:
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
    from fastapi.params import Depends as DependsParam
    from fastapi.responses import JSONResponse
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise ImportError('tele_fastapi.fastapi needs FastAPI: pip install "tele-fastapi[fastapi]"') from e

from pydantic import BaseModel

from . import _base as b
from .async_client import AsyncTeleClient
from .models import JoinUser
from .tokens import TeleSigner, TeleUser
from .webhooks import (
    WEBHOOK_TOLERANCE_SECONDS,
    TeleEvent,
    WebhookPayloadError,
    WebhookSecretError,
    WebhookVerificationError,
    parse_event,
    verify_webhook,
)

__all__ = [
    "TeleClientDep",
    "TeleWebhook",
    "TokenResponse",
    "get_tele",
    "tele_join_router",
    "tele_lifespan",
    "tele_token_router",
]

UserDependency = Callable[..., Any] | DependsParam
"""A FastAPI dependency (or ``Depends(...)``) that returns the signed-in :class:`TeleUser`."""

_STATE_KEY = "tele_client"


def _depends(get_user: UserDependency) -> Any:
    return get_user if isinstance(get_user, DependsParam) else Depends(get_user)


# ---- shared client ----


def tele_lifespan(
    client: AsyncTeleClient | Callable[[], AsyncTeleClient],
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """A FastAPI ``lifespan`` that opens one :class:`AsyncTeleClient` for the app and closes it at shutdown.

    >>> app = FastAPI(lifespan=tele_lifespan(lambda: AsyncTeleClient(signer)))

    To combine it with your own lifespan, enter it inside yours: ``async with tele_lifespan(...)(app): yield``.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tele = client() if callable(client) and not isinstance(client, AsyncTeleClient) else client
        setattr(app.state, _STATE_KEY, tele)
        try:
            yield
        finally:
            await tele.aclose()
            setattr(app.state, _STATE_KEY, None)

    return lifespan


def get_tele(request: Request) -> AsyncTeleClient:
    """Dependency: the app's shared :class:`AsyncTeleClient` (set up by :func:`tele_lifespan`)."""
    tele = getattr(request.app.state, _STATE_KEY, None)
    if not isinstance(tele, AsyncTeleClient):
        raise RuntimeError("tele: no AsyncTeleClient on app.state; create the app with lifespan=tele_lifespan(...)")
    return tele


TeleClientDep = Annotated[AsyncTeleClient, Depends(get_tele)]
"""``tele: TeleClientDep`` in a route signature gives you the shared client."""


# ---- token endpoint ----


class TokenResponse(BaseModel):
    token: str
    expires_at: datetime


def tele_token_router(
    signer: TeleSigner,
    get_user: UserDependency,
    *,
    path: str = "/tele/token",
    ttl: float | timedelta = 900,
    tags: Sequence[str] = ("tele",),
) -> APIRouter:
    """``POST {path}`` returns ``{"token", "expires_at"}``: a Tele user token for the signed-in user.

    ``get_user`` is your own authentication: a dependency that returns a :class:`TeleUser` (or raises 401).
    """
    seconds = b.ttl_seconds(ttl)
    signer.service_token(1)  # fail at startup, not on the first request, if the signer is unusable
    router = APIRouter(tags=list(tags))
    user_dep = _depends(get_user)

    @router.post(path, response_model=TokenResponse, summary="Get a Tele user token")
    def tele_token(user: TeleUser = user_dep) -> TokenResponse:
        token = signer.user_token(user, seconds)
        exp = jwt.decode(token, options={"verify_signature": False})["exp"]
        expires = datetime.fromtimestamp(exp, tz=timezone.utc)
        return TokenResponse(token=token, expires_at=expires)

    return router


# ---- join proxy ----

MAX_DIAGNOSTIC_BYTES = 64 * 1024


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _error(status: int, code: str, message: str) -> JSONResponse:
    """An error in Tele's own shape, so the call UI handles it like any Tele error."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _relay(upstream: httpx.Response) -> Response:
    if upstream.status_code == 401:
        # Our backend's key or secret was refused: not something the app's user can fix.
        return _error(502, "upstream_auth", "the backend's Tele credentials were rejected")
    if upstream.status_code >= 500:
        code = ""
        with suppress(ValueError, KeyError, TypeError):
            code = str(upstream.json()["error"]["code"])
        return _error(502, "upstream_error", f"Tele answered {upstream.status_code} {code}".strip())
    if upstream.status_code == 204 or not upstream.content:
        return Response(status_code=upstream.status_code)
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


async def _forward(send: Callable[[], Awaitable[httpx.Response]]) -> Response:
    try:
        return _relay(await send())
    except httpx.TimeoutException:
        return _error(503, "upstream_timeout", "Tele did not answer in time")
    except httpx.HTTPError as e:
        return _error(502, "upstream_unreachable", f"could not reach Tele ({type(e).__name__})")


def tele_join_router(
    get_user: UserDependency,
    *,
    client: AsyncTeleClient | None = None,
    prefix: str = "/tele",
    user_token_ttl: float | timedelta = 900,
    default_platform: str = "web",
    client_ip: Callable[[Request], str | None] = _client_ip,
    service_join: bool = True,
    diagnostics: bool = True,
    tags: Sequence[str] = ("tele",),
) -> APIRouter:
    """Routes your web and mobile apps call to join, so they never hold a Tele token themselves:

    - ``POST {prefix}/invites/{code}/join``
    - ``POST {prefix}/calls/{call_id}/join``
    - ``POST {prefix}/calls/{call_id}/diagnostics`` (``diagnostics=True``; the ``<tele-call>`` diagnostics body)

    Each request runs as the signed-in user with their user agent and ``X-Client-Platform`` (default
    ``default_platform``). Tele's answer is returned unchanged, so ``getJoin()`` in the call UI gets exactly the
    join response or Tele's error body, with three exceptions in the same ``{"error": {"code", "message"}}`` shape:
    a Tele ``401`` (your backend's credentials) becomes ``502 upstream_auth``, a Tele ``5xx`` becomes
    ``502 upstream_error``, and network failures become ``502 upstream_unreachable`` / ``503 upstream_timeout``.

    **Client IPs.** Hosted Tele does not trust your backend as a proxy, so it ignores ``X-Forwarded-For`` from it.

    - Call joins (``service_join=True``, the default) use your service token with the user and their IP in the
      body, which Tele always records. Set ``service_join=False`` only if Tele trusts your backend as a proxy.
    - Invite joins must use the user's token (Tele refuses service tokens there). Tele then records **your
      backend's IP**, not the user's, unless the app joins Tele directly with a user token from
      :func:`tele_token_router`, or your backend is a proxy Tele trusts. ``X-Forwarded-For`` is still sent.

    ``client`` defaults to the app's shared client (:func:`tele_lifespan`); ``client_ip`` defaults to
    ``request.client.host`` (run uvicorn with ``--proxy-headers`` behind a reverse proxy).
    """
    ttl = b.ttl_seconds(user_token_ttl)
    router = APIRouter(prefix=prefix, tags=list(tags))
    user_dep = _depends(get_user)

    def tele_for(request: Request) -> AsyncTeleClient:
        return client if client is not None else get_tele(request)

    def headers(request: Request) -> tuple[str, str | None, str | None]:
        platform = request.headers.get("x-client-platform", "").strip().lower() or default_platform
        return platform, client_ip(request), request.headers.get("user-agent")

    @router.post("/invites/{code}/join", summary="Join a Tele call through an invite")
    async def join_invite(code: str, request: Request, user: TeleUser = user_dep) -> Response:
        try:
            path = f"/v1/invites/{b.seg(code)}/join"
        except ValueError:
            return _error(404, "invite_not_found", "invite not found")
        platform, ip, ua = headers(request)
        tele = tele_for(request).as_user(user, ttl=ttl)
        return await _forward(lambda: tele.raw_request("POST", path, headers=b.join_headers(platform, ip, ua)))

    @router.post("/calls/{call_id}/join", summary="Join a Tele call you are a member of")
    async def join_call(call_id: str, request: Request, user: TeleUser = user_dep) -> Response:
        try:
            path = f"/v1/calls/{b.seg(call_id)}/join"
        except ValueError:
            return _error(404, "not_found", "call not found")
        platform, ip, ua = headers(request)
        if not service_join:
            as_user = tele_for(request).as_user(user, ttl=ttl)
            return await _forward(lambda: as_user.raw_request("POST", path, headers=b.join_headers(platform, ip, ua)))
        if not ip or not _is_ip(ip):
            return _error(400, "invalid_request", "the client's IP address is unknown")
        joiner = JoinUser(
            user_id=user.id,
            ip=ip,
            name=user.name or None,
            team=user.team or None,
            user_agent=ua[:512] if ua else None,
        )
        tele = tele_for(request)
        return await _forward(
            lambda: tele.raw_request(
                "POST", path, json={"user": b.join_user_input(joiner)}, headers=b.join_headers(platform, None, None)
            )
        )

    if diagnostics:

        @router.post("/calls/{call_id}/diagnostics", summary="Forward a call diagnostic report")
        async def report_diagnostic(call_id: str, request: Request, user: TeleUser = user_dep) -> Response:
            try:
                path = f"/v1/calls/{b.seg(call_id)}/diagnostics"
            except ValueError:
                return _error(404, "not_found", "call not found")
            too_large = _error(413, "invalid_body", f"diagnostic report over {MAX_DIAGNOSTIC_BYTES} bytes")
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_DIAGNOSTIC_BYTES:
                return too_large
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_DIAGNOSTIC_BYTES:
                    return too_large
                chunks.append(chunk)
            body = b"".join(chunks)
            tele = tele_for(request).as_user(user, ttl=ttl)
            return await _forward(
                lambda: tele.raw_request("POST", path, content=body, headers={"Content-Type": "application/json"})
            )

    return router


# ---- webhooks ----

SecretSource = str | Sequence[str] | Callable[[], str | Sequence[str] | None]


class TeleWebhook:
    """Dependency that verifies a Tele webhook against the raw body and returns the typed event.

    >>> @app.post("/tele/webhooks", status_code=204, response_model=None)
    ... async def hook(event: TeleEvent = Depends(TeleWebhook(os.environ["TELE_WEBHOOK_SECRET"]))):
    ...     if isinstance(event, CallEndedEvent): ...

    ``secret`` is a secret, several secrets, or a callable returning them (for secrets you load or rotate at
    runtime). A bad or stale signature answers ``401``, a body that is not a Tele event ``400``, no secret at all
    ``503``, and secrets that are all malformed (not ``whsec_`` + base64) ``500``: that is a configuration error.
    """

    def __init__(self, secret: SecretSource, *, tolerance: int = WEBHOOK_TOLERANCE_SECONDS) -> None:
        self._secret = secret
        self._tolerance = tolerance

    def secrets(self) -> list[str]:
        s = self._secret() if callable(self._secret) else self._secret
        if s is None:
            return []
        return [s] if isinstance(s, str) else [x for x in s if x]

    async def __call__(self, request: Request) -> TeleEvent:
        secrets = self.secrets()
        if not secrets:
            raise HTTPException(503, "webhook secret not configured")
        body = await request.body()
        try:
            verify_webhook(secrets, request.headers, body, tolerance=self._tolerance)
        except WebhookSecretError as e:
            raise HTTPException(500, f"Tele webhook misconfigured: {e}") from None
        except WebhookVerificationError as e:
            raise HTTPException(401, str(e)) from None
        try:
            return parse_event(body)
        except WebhookPayloadError as e:
            raise HTTPException(400, str(e)) from None
