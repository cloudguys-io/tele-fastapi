"""Asynchronous Tele API client.

Same methods and models as :class:`~tele_fastapi.TeleClient`, awaited. The endpoint methods between the
``endpoints`` markers are generated from :mod:`tele_fastapi.client` by ``scripts/gen_async.py``.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from types import TracebackType
from typing import Any

import httpx

from . import _base as b
from .models import (
    APIClient,
    Call,
    CallMember,
    CallUser,
    ChatMessage,
    Invite,
    InviteOptions,
    InvitePreview,
    JoinResponse,
    JoinUser,
    Me,
    PostedMessage,
    Rate,
    Recording,
    RotatedWebhookSecret,
    Session,
    UsageReport,
    WebhookDelivery,
    WebhookDeliveryRetry,
    WebhookEndpoint,
    WebhookTestResult,
)
from .tokens import TeleSigner, TeleUser

__all__ = ["AsyncRecordingStream", "AsyncTeleClient"]


class AsyncRecordingStream(b.RecordingStreamBase):
    """A recording download in progress. Read it with :meth:`aiter_bytes`, then :meth:`aclose` it."""

    async def aclose(self) -> None:
        await self.response.aclose()

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        async for chunk in self.response.aiter_bytes(chunk_size):
            yield chunk

    async def read(self) -> bytes:
        return await self.response.aread()


class AsyncTeleClient:
    """Talks to the Tele API from your async backend (FastAPI, Starlette, aiohttp...).

    Build it from a :class:`~tele_fastapi.TeleSigner` to sign service tokens automatically, then call
    :meth:`as_user` for a client that acts as one of your users. Or pass ``token=``.

    >>> async with AsyncTeleClient(TeleSigner(api_key, api_secret)) as tele:
    ...     call = await tele.create_call("Support", host=CallUser(user_id="agent-7"))
    """

    def __init__(
        self,
        signer: TeleSigner | None = None,
        *,
        token: str | None = None,
        base_url: str = b.DEFAULT_BASE_URL,
        timeout: float | httpx.Timeout = b.DEFAULT_TIMEOUT,
        http_client: httpx.AsyncClient | None = None,
        service_token_ttl: float | timedelta = 300,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if (signer is None) == (token is None):
            raise ValueError("tele: pass exactly one of signer or token")
        self.signer = signer
        self.base_url = base_url.rstrip("/")
        self._credentials: b.Credentials = (
            b.ServiceTokens(signer, b.ttl_seconds(service_token_ttl), 60) if signer else b.StaticToken(token or "")
        )
        self._headers = {"User-Agent": b.USER_AGENT, **(headers or {})}
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    # ---- derived clients ----

    def _derive(
        self, credentials: b.Credentials | None = None, headers: Mapping[str, str] | None = None
    ) -> AsyncTeleClient:
        other = copy.copy(self)
        other._owns_http = False
        if credentials is not None:
            other._credentials = credentials
        other._headers = {**self._headers, **(headers or {})}
        return other

    def as_user(self, user: TeleUser, *, ttl: float | timedelta = 900) -> AsyncTeleClient:
        """A client that acts as ``user`` with a user token (ttl ≤ 1 h), sharing this client's connections."""
        if self.signer is None:
            raise ValueError("tele: as_user needs a client built from a TeleSigner")
        return self._derive(b.UserTokens(self.signer, user, b.ttl_seconds(ttl), 60))

    def with_token(self, token: str) -> AsyncTeleClient:
        """A client that sends ``token`` as is, sharing this client's connections."""
        return self._derive(b.StaticToken(token))

    def for_tenant(self, client_id: str) -> AsyncTeleClient:
        """Admin keys: manage the webhooks of the API key ``client_id`` (sends ``Tele-Client``)."""
        return self._derive(headers={"Tele-Client": client_id})

    def token(self) -> str:
        """The bearer token this client sends right now."""
        return self._credentials.bearer()

    # ---- lifecycle ----

    async def aclose(self) -> None:
        """Close the connection pool (only if this client created it)."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncTeleClient:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    # ---- transport ----

    async def raw_request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        """Send a request and return the response without raising for error statuses.

        ``content`` sends raw bytes (set ``Content-Type`` in ``headers``) instead of ``json``.
        """
        h = b.build_headers(self._credentials if auth else None, {**self._headers, **(headers or {})})
        return await self._http.request(
            method, self.base_url + path, params=params, json=json, content=content, headers=h
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        return b.check(await self.raw_request(method, path, params=params, json=json, headers=headers, auth=auth))

    async def open_recording(self, recording_id: str, *, range: str | None = None) -> AsyncRecordingStream:
        """Start streaming a recording's MP4; you must :meth:`AsyncRecordingStream.aclose` it.

        Pass ``range`` (for example ``"bytes=0-1023"`` or the browser's ``Range`` header) to fetch part of it;
        the answer is then ``206`` with ``content_range``. Raises :class:`TeleError` (``409 archived``,
        ``410 expired``...) before any bytes are read. Handy for proxying to a ``<video>`` tag::

            video = await tele.open_recording(rec_id, range=request.headers.get("range"))
            return StreamingResponse(video.aiter_bytes(), status_code=video.status_code,
                                     headers=video.passthrough_headers(), background=BackgroundTask(video.aclose))
        """
        h = b.build_headers(self._credentials, self._headers)
        h["Accept"] = "video/mp4"
        if range:
            h["Range"] = range
        url = f"{self.base_url}/v1/recordings/{b.seg(recording_id)}/download"
        response = await self._http.send(self._http.build_request("GET", url, headers=h), stream=True)
        if not response.is_success:
            try:
                await response.aread()
            finally:
                await response.aclose()
            b.check(response)
        return AsyncRecordingStream(response)

    @asynccontextmanager
    async def stream_recording(
        self, recording_id: str, *, range: str | None = None
    ) -> AsyncIterator[AsyncRecordingStream]:
        """:meth:`open_recording` as an async context manager.

        >>> async with tele.stream_recording(rec_id) as video:
        ...     async for chunk in video.aiter_bytes(): ...
        """
        video = await self.open_recording(recording_id, range=range)
        try:
            yield video
        finally:
            await video.aclose()

    async def download_recording(self, recording_id: str, *, range: str | None = None) -> bytes:
        """Download a recording (or a ``range`` of it) into memory."""
        async with self.stream_recording(recording_id, range=range) as video:
            return await video.read()

    # ---- endpoints ----

    async def health(self) -> dict[str, Any]:
        """``GET /healthz`` (no auth)."""
        resp = await self._send("GET", "/healthz", auth=False)
        out: dict[str, Any] = resp.json()
        return out

    async def ready(self) -> dict[str, Any]:
        """``GET /readyz`` (no auth). Raises a 503 :class:`TeleError` when the database is unavailable."""
        resp = await self._send("GET", "/readyz", auth=False)
        out: dict[str, Any] = resp.json()
        return out

    async def me(self) -> Me:
        """``GET /v1/me``: who the token represents."""
        return b.model(Me, await self._send("GET", "/v1/me"))

    # calls

    async def create_call(
        self,
        title: str,
        *,
        host: CallUser | Mapping[str, Any] | None = None,
        members: Sequence[CallUser | Mapping[str, Any]] | None = None,
        access_policy: str | None = None,
        scheduled_start: datetime | None = None,
        scheduled_end: datetime | None = None,
        join_opens_before_minutes: int | None = None,
        join_closes_after_minutes: int | None = None,
        max_participants: int | None = None,
        max_duration_minutes: int | None = None,
        record: bool | None = None,
        invite: InviteOptions | Mapping[str, Any] | None = None,
    ) -> Call:
        """``POST /v1/calls``. With a service token ``host`` is required; with a user token the token's user hosts.

        The returned call carries ``members`` and the default ``invite`` with its one-time ``code``.
        """
        data = b.body(
            title=title,
            access_policy=access_policy,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            join_opens_before_minutes=join_opens_before_minutes,
            join_closes_after_minutes=join_closes_after_minutes,
            max_participants=max_participants,
            max_duration_minutes=max_duration_minutes,
            record=record,
            host=b.user_input(host) if host is not None else None,
            members=[b.user_input(m) for m in members] if members is not None else None,
            invite=b.invite_input(invite) if invite is not None else None,
        )
        return b.model(Call, await self._send("POST", "/v1/calls", json=data))

    async def list_calls(
        self,
        *,
        status: str | Sequence[str] | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
        limit: int | None = None,
    ) -> list[Call]:
        """``GET /v1/calls``, newest first. A user token sees the calls it is a member of."""
        st = status if status is None or isinstance(status, str) else ",".join(status)
        q = b.params(status=st, limit=limit, **{"from": from_, "to": to})
        return b.models(Call, await self._send("GET", "/v1/calls", params=q), "calls")

    async def get_call(self, call_id: str) -> Call:
        """``GET /v1/calls/{id}``: the call with its members."""
        return b.model(Call, await self._send("GET", f"/v1/calls/{b.seg(call_id)}"))

    async def update_call(
        self,
        call_id: str,
        *,
        title: str | None = None,
        access_policy: str | None = None,
        scheduled_start: datetime | None = None,
        scheduled_end: datetime | None = None,
        max_participants: int | None = None,
        record: bool | None = None,
    ) -> Call:
        """``PATCH /v1/calls/{id}``: change any of the given fields. ``409 call_closed`` once ended or cancelled."""
        data = b.body(
            title=title,
            access_policy=access_policy,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            max_participants=max_participants,
            record=record,
        )
        return b.model(Call, await self._send("PATCH", f"/v1/calls/{b.seg(call_id)}", json=data))

    async def cancel_call(self, call_id: str) -> Call:
        """``DELETE /v1/calls/{id}``: cancel the call; anyone connected is disconnected."""
        return b.model(Call, await self._send("DELETE", f"/v1/calls/{b.seg(call_id)}"))

    async def end_call(self, call_id: str) -> Call:
        """``POST /v1/calls/{id}/end``: end the call now for everyone."""
        return b.model(Call, await self._send("POST", f"/v1/calls/{b.seg(call_id)}/end"))

    async def put_member(
        self, call_id: str, user_id: str, *, name: str | None = None, team: str | None = None
    ) -> list[CallMember]:
        """``PUT /v1/calls/{id}/members/{user_id}``: add or update a participant. Returns all members."""
        data = b.body(name=name, team=team)
        r = await self._send("PUT", f"/v1/calls/{b.seg(call_id)}/members/{b.seg(user_id)}", json=data)
        return b.models(CallMember, r, "members")

    async def remove_member(self, call_id: str, user_id: str) -> None:
        """``DELETE /v1/calls/{id}/members/{user_id}`` (the host can't be removed)."""
        await self._send("DELETE", f"/v1/calls/{b.seg(call_id)}/members/{b.seg(user_id)}")

    async def list_sessions(self, call_id: str) -> list[Session]:
        """``GET /v1/calls/{id}/sessions``: room sessions with participants, IPs, durations and charges."""
        return b.models(Session, await self._send("GET", f"/v1/calls/{b.seg(call_id)}/sessions"), "sessions")

    # invites

    async def create_invite(
        self,
        call_id: str,
        *,
        label: str | None = None,
        expires_at: datetime | None = None,
        max_uses: int | None = None,
    ) -> Invite:
        """``POST /v1/calls/{id}/invites``. The returned ``code`` is shown only now."""
        data = b.body(label=label, expires_at=expires_at, max_uses=max_uses)
        return b.model(Invite, await self._send("POST", f"/v1/calls/{b.seg(call_id)}/invites", json=data))

    async def list_invites(self, call_id: str) -> list[Invite]:
        """``GET /v1/calls/{id}/invites`` (without codes)."""
        return b.models(Invite, await self._send("GET", f"/v1/calls/{b.seg(call_id)}/invites"), "invites")

    async def revoke_invite(self, invite_id: str) -> None:
        """``DELETE /v1/invites/{invite_id}``."""
        await self._send("DELETE", f"/v1/invites/{b.seg(invite_id)}")

    async def preview_invite(self, code: str) -> InvitePreview:
        """``GET /v1/invites/{code}``: call summary, ``can_join_now`` and ``reason`` for the current user."""
        return b.model(InvitePreview, await self._send("GET", f"/v1/invites/{b.seg(code)}"))

    # joining

    async def join_invite(
        self,
        code: str,
        *,
        platform: str | None = None,
        forwarded_for: str | None = None,
        user_agent: str | None = None,
    ) -> JoinResponse:
        """``POST /v1/invites/{code}/join`` with a user token.

        ``platform`` is sent as ``X-Client-Platform``. When your backend is a trusted proxy for Tele,
        ``forwarded_for`` (the end user's IP, sent as ``X-Forwarded-For``) and ``user_agent`` record the real client.
        """
        h = b.join_headers(platform, forwarded_for, user_agent)
        return b.model(JoinResponse, await self._send("POST", f"/v1/invites/{b.seg(code)}/join", headers=h))

    async def join_call(
        self,
        call_id: str,
        *,
        user: JoinUser | Mapping[str, Any] | None = None,
        platform: str | None = None,
        forwarded_for: str | None = None,
        user_agent: str | None = None,
    ) -> JoinResponse:
        """``POST /v1/calls/{id}/join``.

        With a user token, joins as that user (a member, or anyone for ``any_client_user`` calls). With a service
        token, pass ``user`` (``user_id`` and the end user's ``ip`` are required) to join on their behalf.
        """
        h = b.join_headers(platform, forwarded_for, user_agent)
        data = {"user": b.join_user_input(user)} if user is not None else None
        return b.model(JoinResponse, await self._send("POST", f"/v1/calls/{b.seg(call_id)}/join", json=data, headers=h))

    # chat

    async def list_messages(
        self, call_id: str, *, after: str | None = None, limit: int | None = None
    ) -> list[ChatMessage]:
        """``GET /v1/calls/{id}/messages``. Page with ``after=<last message id>``; ``limit`` ≤ 500."""
        q = b.params(after=after, limit=limit)
        return b.models(
            ChatMessage, await self._send("GET", f"/v1/calls/{b.seg(call_id)}/messages", params=q), "messages"
        )

    async def post_message(
        self, call_id: str, stream_id: str, body: str, *, sent_at: datetime | None = None
    ) -> PostedMessage:
        """``POST /v1/calls/{id}/messages`` (user token). Deduplicated by ``stream_id``."""
        data = b.body(stream_id=stream_id, body=body, sent_at=sent_at)
        return b.model(PostedMessage, await self._send("POST", f"/v1/calls/{b.seg(call_id)}/messages", json=data))

    # recordings

    async def start_recording(self, call_id: str) -> Recording:
        """``POST /v1/calls/{id}/recording/start``: record a live call."""
        return b.model(Recording, await self._send("POST", f"/v1/calls/{b.seg(call_id)}/recording/start"))

    async def stop_recording(self, call_id: str) -> Recording:
        """``POST /v1/calls/{id}/recording/stop``."""
        return b.model(Recording, await self._send("POST", f"/v1/calls/{b.seg(call_id)}/recording/stop"))

    async def list_recordings(self, call_id: str) -> list[Recording]:
        """``GET /v1/calls/{id}/recordings``."""
        return b.models(Recording, await self._send("GET", f"/v1/calls/{b.seg(call_id)}/recordings"), "recordings")

    async def get_recording(self, recording_id: str) -> Recording:
        """``GET /v1/recordings/{id}``."""
        return b.model(Recording, await self._send("GET", f"/v1/recordings/{b.seg(recording_id)}"))

    async def restore_recording(self, recording_id: str) -> Recording:
        """``POST /v1/recordings/{id}/restore``: thaw an archived recording (downloadable for 7 days after)."""
        return b.model(Recording, await self._send("POST", f"/v1/recordings/{b.seg(recording_id)}/restore"))

    # diagnostics

    async def report_diagnostic(self, call_id: str, report: Mapping[str, Any]) -> None:
        """``POST /v1/calls/{id}/diagnostics`` (user token): forward a ``<tele-call>`` diagnostic body as is."""
        await self._send("POST", f"/v1/calls/{b.seg(call_id)}/diagnostics", json=dict(report))

    # webhooks

    async def create_webhook(
        self, url: str, events: Sequence[str], *, description: str | None = None
    ) -> WebhookEndpoint:
        """``POST /v1/webhooks``. The returned ``secret`` is shown only now. ``events=["*"]`` for everything."""
        data = b.body(url=url, events=list(events), description=description)
        return b.model(WebhookEndpoint, await self._send("POST", "/v1/webhooks", json=data))

    async def list_webhooks(self) -> list[WebhookEndpoint]:
        """``GET /v1/webhooks``."""
        return b.models(WebhookEndpoint, await self._send("GET", "/v1/webhooks"), "webhooks")

    async def get_webhook(self, webhook_id: str) -> WebhookEndpoint:
        """``GET /v1/webhooks/{id}``, including ``failing_since``."""
        return b.model(WebhookEndpoint, await self._send("GET", f"/v1/webhooks/{b.seg(webhook_id)}"))

    async def update_webhook(
        self,
        webhook_id: str,
        *,
        url: str | None = None,
        events: Sequence[str] | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> WebhookEndpoint:
        """``PATCH /v1/webhooks/{id}``. ``enabled=True`` re-enables an auto-disabled endpoint."""
        data = b.body(
            url=url, events=list(events) if events is not None else None, description=description, enabled=enabled
        )
        return b.model(WebhookEndpoint, await self._send("PATCH", f"/v1/webhooks/{b.seg(webhook_id)}", json=data))

    async def delete_webhook(self, webhook_id: str) -> None:
        """``DELETE /v1/webhooks/{id}``: remove the endpoint and its delivery log."""
        await self._send("DELETE", f"/v1/webhooks/{b.seg(webhook_id)}")

    async def rotate_webhook_secret(self, webhook_id: str) -> RotatedWebhookSecret:
        """``POST /v1/webhooks/{id}/rotate-secret``: a new secret; the old one keeps signing for 24 hours."""
        return b.model(
            RotatedWebhookSecret, await self._send("POST", f"/v1/webhooks/{b.seg(webhook_id)}/rotate-secret")
        )

    async def test_webhook(self, webhook_id: str) -> WebhookTestResult:
        """``POST /v1/webhooks/{id}/test``: queue a ``webhook.test`` event."""
        return b.model(WebhookTestResult, await self._send("POST", f"/v1/webhooks/{b.seg(webhook_id)}/test"))

    async def list_webhook_deliveries(
        self, webhook_id: str, *, status: str | None = None, limit: int | None = None
    ) -> list[WebhookDelivery]:
        """``GET /v1/webhooks/{id}/deliveries``. ``status``: ``pending``, ``delivered`` or ``failed``."""
        q = b.params(status=status, limit=limit)
        r = await self._send("GET", f"/v1/webhooks/{b.seg(webhook_id)}/deliveries", params=q)
        return b.models(WebhookDelivery, r, "deliveries")

    async def retry_webhook_delivery(self, delivery_id: str) -> WebhookDeliveryRetry:
        """``POST /v1/webhook-deliveries/{id}/retry``: send a delivery again now."""
        return b.model(
            WebhookDeliveryRetry, await self._send("POST", f"/v1/webhook-deliveries/{b.seg(delivery_id)}/retry")
        )

    # tenant API keys (admin)

    async def create_client(self, name: str, owner_ref: str, *, invite_url_template: str | None = None) -> APIClient:
        """``POST /v1/clients``: a new API key for one of your tenants. ``api_secret`` is shown only now."""
        data = b.body(name=name, owner_ref=owner_ref, invite_url_template=invite_url_template)
        return b.model(APIClient, await self._send("POST", "/v1/clients", json=data))

    async def list_clients(self, owner_ref: str) -> list[APIClient]:
        """``GET /v1/clients?owner_ref=``: one tenant's keys, revoked ones too."""
        return b.models(
            APIClient, await self._send("GET", "/v1/clients", params=b.params(owner_ref=owner_ref)), "clients"
        )

    async def revoke_client(self, client_id: str) -> None:
        """``DELETE /v1/clients/{id}``: revoke a tenant key at once. Its calls and usage stay."""
        await self._send("DELETE", f"/v1/clients/{b.seg(client_id)}")

    async def rotate_client_secret(self, client_id: str) -> APIClient:
        """``POST /v1/clients/{id}/rotate-secret``: the old secret stops working at once."""
        return b.model(APIClient, await self._send("POST", f"/v1/clients/{b.seg(client_id)}/rotate-secret"))

    # usage and rates

    async def usage(
        self,
        *,
        from_: date | datetime | str | None = None,
        to: date | datetime | str | None = None,
        group_by: str | None = None,
        team: str | None = None,
        api_key: str | None = None,
    ) -> UsageReport:
        """``GET /v1/usage`` (service token). ``group_by``: ``client``, ``team``, ``user``, ``call`` or ``month``."""
        q = b.params(group_by=group_by, team=team, api_key=api_key, **{"from": from_, "to": to})
        return b.model(UsageReport, await self._send("GET", "/v1/usage", params=q))

    async def usage_csv(
        self,
        *,
        from_: date | datetime | str | None = None,
        to: date | datetime | str | None = None,
        group_by: str | None = None,
        team: str | None = None,
        api_key: str | None = None,
    ) -> str:
        """``GET /v1/usage?format=csv``: the same report as CSV text."""
        q = b.params(group_by=group_by, team=team, api_key=api_key, format="csv", **{"from": from_, "to": to})
        resp = await self._send("GET", "/v1/usage", params=q, headers={"Accept": "text/csv"})
        return resp.text

    async def list_rates(self, *, active: bool = False) -> list[Rate]:
        """``GET /v1/rates`` (admin). ``active=True`` for the rates in effect now."""
        q = b.params(active=True) if active else None
        return b.models(Rate, await self._send("GET", "/v1/rates", params=q), "rates")

    async def set_rate(
        self,
        name: str,
        room_minute_price: str,
        currency: str,
        *,
        api_key: str | None = None,
        team: str | None = None,
        recording_minute_price: str | None = None,
        rounding: str | None = None,
        min_billable_minutes: int | None = None,
        min_participants: int | None = None,
        effective_from: datetime | None = None,
    ) -> Rate:
        """``POST /v1/rates`` (admin): set the rate for a scope (the org default, a key, or a key's team).

        Prices are decimal strings with up to 4 places, for example ``"1.5"``.
        """
        data = b.body(
            name=name,
            room_minute_price=room_minute_price,
            currency=currency,
            api_key=api_key,
            team=team,
            recording_minute_price=recording_minute_price,
            rounding=rounding,
            min_billable_minutes=min_billable_minutes,
            min_participants=min_participants,
            effective_from=effective_from,
        )
        return b.model(Rate, await self._send("POST", "/v1/rates", json=data))

    async def end_rate(self, rate_id: str, *, effective_to: datetime | None = None) -> Rate:
        """``PATCH /v1/rates/{id}`` (admin): end a key or team override now or at ``effective_to``."""
        return b.model(
            Rate, await self._send("PATCH", f"/v1/rates/{b.seg(rate_id)}", json=b.body(effective_to=effective_to))
        )

    # ---- end endpoints ----
