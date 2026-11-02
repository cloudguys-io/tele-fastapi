"""FastAPI integration: a token endpoint, a join proxy, webhook verification and a shared client.

Needs the ``fastapi`` extra: ``pip install "tele-fastapi[fastapi]"``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

try:
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
    from fastapi.params import Depends as DependsParam
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
        expires = datetime.fromtimestamp(signer.now() + int(seconds), tz=timezone.utc)
        return TokenResponse(token=token, expires_at=expires)

    return router


# ---- join proxy ----


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _relay(upstream: Any) -> Response:
    if upstream.status_code == 204 or not upstream.content:
        return Response(status_code=upstream.status_code)
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


def tele_join_router(
    get_user: UserDependency,
    *,
    client: AsyncTeleClient | None = None,
    prefix: str = "/tele",
    user_token_ttl: float | timedelta = 900,
    default_platform: str = "web",
    client_ip: Callable[[Request], str | None] = _client_ip,
    service_join: bool = False,
    diagnostics: bool = True,
    tags: Sequence[str] = ("tele",),
) -> APIRouter:
    """Routes your web and mobile apps call to join, so they never hold a Tele token themselves:

    - ``POST {prefix}/invites/{code}/join``
    - ``POST {prefix}/calls/{call_id}/join``
    - ``POST {prefix}/calls/{call_id}/diagnostics`` (``diagnostics=True``; the ``<tele-call>`` diagnostics body)

    Each request is forwarded as the signed-in user with their real IP (``X-Forwarded-For``), user agent and
    ``X-Client-Platform`` (default ``default_platform``). Tele's response, success or error, is returned unchanged,
    so ``getJoin()`` in the call UI gets exactly the join response or Tele error body.

    Tele only trusts ``X-Forwarded-For`` from proxies it is configured to trust. If your backend is not one,
    set ``service_join=True``: call joins then use a service token with the user's IP in the body, which Tele
    always records. Invite joins always use the user's token. ``client`` defaults to the app's shared client
    (:func:`tele_lifespan`); ``client_ip`` defaults to ``request.client.host`` (run uvicorn with
    ``--proxy-headers`` behind a reverse proxy).
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
        platform, ip, ua = headers(request)
        tele = tele_for(request).as_user(user, ttl=ttl)
        upstream = await tele.raw_request(
            "POST", f"/v1/invites/{b.seg(code)}/join", headers=b.join_headers(platform, ip, ua)
        )
        return _relay(upstream)

    @router.post("/calls/{call_id}/join", summary="Join a Tele call you are a member of")
    async def join_call(call_id: str, request: Request, user: TeleUser = user_dep) -> Response:
        platform, ip, ua = headers(request)
        path = f"/v1/calls/{b.seg(call_id)}/join"
        if service_join:
            if not ip:
                raise HTTPException(400, "client IP unknown")
            joiner = JoinUser(
                user_id=user.id,
                ip=ip,
                name=user.name or None,
                team=user.team or None,
                user_agent=ua[:512] if ua else None,
            )
            upstream = await tele_for(request).raw_request(
                "POST", path, json={"user": b.join_user_input(joiner)}, headers=b.join_headers(platform, None, None)
            )
        else:
            tele = tele_for(request).as_user(user, ttl=ttl)
            upstream = await tele.raw_request("POST", path, headers=b.join_headers(platform, ip, ua))
        return _relay(upstream)

    if diagnostics:

        @router.post("/calls/{call_id}/diagnostics", summary="Forward a call diagnostic report")
        async def report_diagnostic(call_id: str, request: Request, user: TeleUser = user_dep) -> Response:
            body = await request.body()
            if len(body) > 64 * 1024:
                raise HTTPException(413, "diagnostic report too large")
            tele = tele_for(request).as_user(user, ttl=ttl)
            upstream = await tele.raw_request(
                "POST",
                f"/v1/calls/{b.seg(call_id)}/diagnostics",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            return _relay(upstream)

    return router


# ---- webhooks ----

SecretSource = str | Sequence[str] | Callable[[], str | Sequence[str] | None]


class TeleWebhook:
    """Dependency that verifies a Tele webhook against the raw body and returns the typed event.

    >>> @app.post("/tele/webhooks", status_code=204)
    ... async def hook(event: TeleEvent = Depends(TeleWebhook(os.environ["TELE_WEBHOOK_SECRET"]))):
    ...     if isinstance(event, CallEndedEvent): ...

    ``secret`` is a secret, several secrets, or a callable returning them (for secrets you load or rotate at
    runtime). A bad or stale signature answers ``401``, a body that is not a Tele event ``400``, and a missing
    secret ``503``.
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
        except WebhookVerificationError as e:
            raise HTTPException(401, str(e)) from None
        try:
            return parse_event(body)
        except WebhookPayloadError as e:
            raise HTTPException(400, str(e)) from None
