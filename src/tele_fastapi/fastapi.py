"""FastAPI integration: a token endpoint, a join proxy, webhook verification and a shared client.

Needs the ``fastapi`` extra: ``pip install "tele-fastapi[fastapi]"``.
"""

# No ``from __future__ import annotations`` here: FastAPI before 0.124 can't resolve string annotations on a
# dependency class's ``__call__``, so these annotations must be real objects at runtime.

import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
import jwt

try:
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
    from fastapi.exceptions import RequestValidationError
    from fastapi.params import Depends as DependsParam
    from fastapi.responses import JSONResponse
    from fastapi.routing import APIRoute
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise ImportError('tele_fastapi.fastapi needs FastAPI: pip install "tele-fastapi[fastapi]"') from e

from pydantic import BaseModel

from . import _base as b
from .async_client import AsyncTeleClient
from .errors import TeleError
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
    "DEFAULT_PREFIX",
    "TeleClientDep",
    "TeleErrorRoute",
    "TeleWebhook",
    "TokenResponse",
    "get_tele",
    "tele_error_response",
    "tele_join_router",
    "tele_lifespan",
    "tele_token_router",
]

DEFAULT_PREFIX = "/api/tele"
"""The shared backend route prefix the Tele UI and client SDKs call by default."""

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


# ---- errors in Tele's shape ----

_CODES = {
    400: "invalid_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    413: "invalid_body",
    422: "invalid_request",
    429: "rate_limited",
}


def _error(status: int, code: str, message: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    """An error in Tele's own shape, so the call UI handles it like any Tele error."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def tele_error_response(err: TeleError) -> JSONResponse:
    """Answer an app with a :class:`TeleError` the way the Tele routers do.

    Tele 4xx errors are relayed unchanged (except ``401``, your backend's own credentials, which becomes
    ``502 upstream_auth``), and Tele ``5xx`` becomes ``502 upstream_error``.
    """
    if err.status == 401:
        return _error(502, "upstream_auth", "the backend's Tele credentials were rejected")
    if err.status >= 500:
        return _error(502, "upstream_error", f"Tele answered {err.status} {err.code}")
    if isinstance(err.body, dict) and isinstance(err.body.get("error"), dict):
        return JSONResponse(err.body, status_code=err.status)
    return _error(err.status, err.code, err.message)


class TeleErrorRoute(APIRoute):
    """Route class that answers ``HTTPException`` and validation errors in Tele's ``{"error": {...}}`` shape.

    The Tele routers use it, so a ``get_user`` that raises ``HTTPException(401)`` gives the app
    ``401 {"error": {"code": "unauthenticated", ...}}``. Use it for your own routers with
    ``APIRouter(route_class=TeleErrorRoute)``.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except HTTPException as e:
                message = e.detail if isinstance(e.detail, str) else "request failed"
                return _error(e.status_code, _CODES.get(e.status_code, "error"), message, e.headers)
            except RequestValidationError as e:
                fields = ", ".join(".".join(str(p) for p in err.get("loc", ())) for err in e.errors())
                return _error(400, "invalid_request", f"invalid {fields}" if fields else "invalid request")

        return handle


# ---- token endpoint ----


class TokenResponse(BaseModel):
    token: str
    expires_at: datetime


def tele_token_router(
    signer: TeleSigner,
    get_user: UserDependency,
    *,
    path: str = DEFAULT_PREFIX + "/token",
    ttl: float | timedelta = 900,
    tags: Sequence[str] = ("tele",),
) -> APIRouter:
    """``POST {path}`` returns ``{"token", "expires_at"}``: a Tele user token for the signed-in user.

    ``get_user`` is your own authentication: a dependency that returns a :class:`TeleUser` (or raises
    ``HTTPException(401)``, which the app receives as ``401 {"error": {"code": "unauthenticated", ...}}``).
    """
    seconds = b.ttl_seconds(ttl)
    signer.service_token(1)  # fail at startup, not on the first request, if the signer is unusable
    router = APIRouter(tags=list(tags), route_class=TeleErrorRoute)
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
    prefix: str = DEFAULT_PREFIX,
    user_token_ttl: float | timedelta = 900,
    default_platform: str = "web",
    client_ip: Callable[[Request], str | None] = _client_ip,
    service_join: bool = True,
    diagnostics: bool = True,
    tags: Sequence[str] = ("tele",),
) -> APIRouter:
    """The shared backend routes (default prefix ``/api/tele``) your web and mobile apps call, so they never hold
    a Tele token themselves:

    - ``GET {prefix}/invites/{code}``: invite preview, as the user
    - ``POST {prefix}/invites/{code}/join``: invite join, as the user
    - ``POST {prefix}/calls/{call_id}/join``: call join (a service-token join carrying the user's IP by default)
    - ``GET {prefix}/calls/{call_id}/messages``: chat history as the user (``after`` and ``limit`` passed through)
    - ``POST {prefix}/calls/{call_id}/diagnostics`` (``diagnostics=True``; the ``<tele-call>`` diagnostics body)

    Each request runs as the signed-in user with their user agent and ``X-Client-Platform`` (default
    ``default_platform``). Tele's answer is returned unchanged, so ``getJoin()`` in the call UI gets exactly the
    join response or Tele's error body, with three exceptions in the same ``{"error": {"code", "message"}}`` shape:
    a Tele ``401`` (your backend's credentials) becomes ``502 upstream_auth``, a Tele ``5xx`` becomes
    ``502 upstream_error``, and network failures become ``502 upstream_unreachable`` / ``503 upstream_timeout``.

    **Client IPs.** Tele records the request's peer IP for user-token calls and trusts no customer proxy on
    hosted Tele.

    - Call-id joins are service-token joins carrying the end user's IP (``service_join=True``, the default), so
      Tele records the correct IP. Set ``service_join=False`` only for a Tele that trusts your backend as a proxy.
    - Invite joins through a backend record the **backend's** IP. Browsers can't avoid this (Tele sends no CORS
      headers); native apps can, by calling ``POST /v1/invites/{code}/join`` directly with a user token from
      :func:`tele_token_router`. After the first invite join, rejoin through the call-id route.

    Not signed in (``get_user`` raises 401) answers ``401 {"error": {"code": "unauthenticated", ...}}``.

    ``client`` defaults to the app's shared client (:func:`tele_lifespan`); ``client_ip`` defaults to
    ``request.client.host`` (run uvicorn with ``--proxy-headers`` behind a reverse proxy).
    """
    ttl = b.ttl_seconds(user_token_ttl)
    router = APIRouter(prefix=prefix, tags=list(tags), route_class=TeleErrorRoute)
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

    @router.get("/invites/{code}", summary="Preview a Tele invite")
    async def preview_invite(code: str, request: Request, user: TeleUser = user_dep) -> Response:
        try:
            path = f"/v1/invites/{b.seg(code)}"
        except ValueError:
            return _error(404, "invite_not_found", "invite not found")
        tele = tele_for(request).as_user(user, ttl=ttl)
        return await _forward(lambda: tele.raw_request("GET", path))

    @router.get("/calls/{call_id}/messages", summary="Chat history of a Tele call")
    async def list_messages(call_id: str, request: Request, user: TeleUser = user_dep) -> Response:
        try:
            path = f"/v1/calls/{b.seg(call_id)}/messages"
        except ValueError:
            return _error(404, "not_found", "call not found")
        query = {k: v for k, v in request.query_params.items() if k in ("after", "limit")}
        tele = tele_for(request).as_user(user, ttl=ttl)
        return await _forward(lambda: tele.raw_request("GET", path, params=query))

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
