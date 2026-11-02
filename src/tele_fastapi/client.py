"""Synchronous Tele API client.

The endpoint methods between the ``endpoints`` markers are the source for :mod:`tele_fastapi.async_client`,
which ``scripts/gen_async.py`` generates from them. Edit them here, then run the script.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
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

__all__ = ["RecordingStream", "TeleClient"]


class RecordingStream(b.RecordingStreamBase):
    """A recording download in progress. Read it with :meth:`iter_bytes`, then :meth:`close` it."""

    def close(self) -> None:
        self.response.close()

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield from self.response.iter_bytes(chunk_size)

    def read(self) -> bytes:
        return self.response.read()


class TeleClient:
    """Talks to the Tele API from your backend.

    Build it from a :class:`~tele_fastapi.TeleSigner` to sign service tokens automatically (minted on demand
    and reused until close to expiry), then call :meth:`as_user` for a client that acts as one of your users.
    Or pass ``token=`` to use a bearer token you already have.

    >>> tele = TeleClient(TeleSigner(api_key, api_secret))
    >>> call = tele.create_call("Support", host=CallUser(user_id="agent-7"))
    >>> join = tele.as_user(TeleUser(id="cust-9")).join_invite(call.invite.code, platform="web")
    """

    def __init__(
        self,
        signer: TeleSigner | None = None,
        *,
        token: str | None = None,
        base_url: str = b.DEFAULT_BASE_URL,
        timeout: float | httpx.Timeout = b.DEFAULT_TIMEOUT,
        http_client: httpx.Client | None = None,
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
        self._http = http_client or httpx.Client(timeout=timeout)

    # ---- derived clients ----

    def _derive(self, credentials: b.Credentials | None = None, headers: Mapping[str, str] | None = None) -> TeleClient:
        other = copy.copy(self)
        other._owns_http = False
        if credentials is not None:
            other._credentials = credentials
        other._headers = {**self._headers, **(headers or {})}
        return other

    def as_user(self, user: TeleUser, *, ttl: float | timedelta = 900) -> TeleClient:
        """A client that acts as ``user`` with a user token (ttl ≤ 1 h), sharing this client's connections."""
        if self.signer is None:
            raise ValueError("tele: as_user needs a client built from a TeleSigner")
        return self._derive(b.UserTokens(self.signer, user, b.ttl_seconds(ttl), 60))

    def with_token(self, token: str) -> TeleClient:
        """A client that sends ``token`` as is, sharing this client's connections."""
        return self._derive(b.StaticToken(token))

    def for_tenant(self, client_id: str) -> TeleClient:
        """Admin keys: manage the webhooks of the API key ``client_id`` (sends ``Tele-Client``)."""
        return self._derive(headers={"Tele-Client": client_id})

    def token(self) -> str:
        """The bearer token this client sends right now."""
        return self._credentials.bearer()

    # ---- lifecycle ----

    def close(self) -> None:
        """Close the connection pool (only if this client created it)."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> TeleClient:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # ---- transport ----

    def raw_request(
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
        return self._http.request(method, self.base_url + path, params=params, json=json, content=content, headers=h)

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        return b.check(self.raw_request(method, path, params=params, json=json, headers=headers, auth=auth))

    def open_recording(self, recording_id: str, *, range: str | None = None) -> RecordingStream:
        """Start streaming a recording's MP4; you must :meth:`RecordingStream.close` it.

        Pass ``range`` (for example ``"bytes=0-1023"`` or the browser's ``Range`` header) to fetch part of it;
        the answer is then ``206`` with ``content_range``. Raises :class:`TeleError` (``409 archived``,
        ``410 expired``...) before any bytes are read.
        """
        h = b.build_headers(self._credentials, self._headers)
        h["Accept"] = "video/mp4"
        if range:
            h["Range"] = range
        url = f"{self.base_url}/v1/recordings/{b.seg(recording_id)}/download"
        response = self._http.send(self._http.build_request("GET", url, headers=h), stream=True)
        if not response.is_success:
            try:
                response.read()
            finally:
                response.close()
            b.check(response)
        return RecordingStream(response)

    @contextmanager
    def stream_recording(self, recording_id: str, *, range: str | None = None) -> Iterator[RecordingStream]:
        """:meth:`open_recording` as a context manager.

        >>> with tele.stream_recording(rec_id) as video:
        ...     for chunk in video.iter_bytes(): out.write(chunk)
        """
        video = self.open_recording(recording_id, range=range)
        try:
            yield video
        finally:
            video.close()

    def download_recording(self, recording_id: str, *, range: str | None = None) -> bytes:
        """Download a recording (or a ``range`` of it) into memory."""
        with self.stream_recording(recording_id, range=range) as video:
            return video.read()

    # ---- endpoints ----

    def health(self) -> dict[str, Any]:
        """``GET /healthz`` (no auth)."""
        resp = self._send("GET", "/healthz", auth=False)
        out: dict[str, Any] = resp.json()
        return out

    def ready(self) -> dict[str, Any]:
        """``GET /readyz`` (no auth). Raises a 503 :class:`TeleError` when the database is unavailable."""
        resp = self._send("GET", "/readyz", auth=False)
        out: dict[str, Any] = resp.json()
        return out

    def me(self) -> Me:
        """``GET /v1/me``: who the token represents."""
        return b.model(Me, self._send("GET", "/v1/me"))

    # calls

    def create_call(
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
        return b.model(Call, self._send("POST", "/v1/calls", json=data))

    def list_calls(
        self,
        *,
        status: str | Sequence[str] | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
        limit: int | None = None,
    ) -> list[Call]:
        """``GET /v1/calls``, newest first (``limit`` 1-200). A user token sees the calls it is a member of."""
        st = status if status is None or isinstance(status, str) else ",".join(status)
        q = b.params(status=st, limit=b.limit(limit, 200), **{"from": from_, "to": to})
        return b.models(Call, self._send("GET", "/v1/calls", params=q), "calls")

    def get_call(self, call_id: str) -> Call:
        """``GET /v1/calls/{id}``: the call with its members."""
        return b.model(Call, self._send("GET", f"/v1/calls/{b.seg(call_id)}"))

    def update_call(
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
        return b.model(Call, self._send("PATCH", f"/v1/calls/{b.seg(call_id)}", json=data))

    def cancel_call(self, call_id: str) -> Call:
        """``DELETE /v1/calls/{id}``: cancel the call; anyone connected is disconnected."""
        return b.model(Call, self._send("DELETE", f"/v1/calls/{b.seg(call_id)}"))

    def end_call(self, call_id: str) -> Call:
        """``POST /v1/calls/{id}/end``: end the call now for everyone."""
        return b.model(Call, self._send("POST", f"/v1/calls/{b.seg(call_id)}/end"))

    def put_member(
        self, call_id: str, user_id: str, *, name: str | None = None, team: str | None = None
    ) -> list[CallMember]:
        """``PUT /v1/calls/{id}/members/{user_id}``: add or update a participant. Returns all members."""
        data = b.body(name=name, team=team)
        r = self._send("PUT", f"/v1/calls/{b.seg(call_id)}/members/{b.seg(user_id)}", json=data)
        return b.models(CallMember, r, "members")

    def remove_member(self, call_id: str, user_id: str) -> None:
        """``DELETE /v1/calls/{id}/members/{user_id}`` (the host can't be removed)."""
        self._send("DELETE", f"/v1/calls/{b.seg(call_id)}/members/{b.seg(user_id)}")

    def list_sessions(self, call_id: str) -> list[Session]:
        """``GET /v1/calls/{id}/sessions``: room sessions with participants, IPs, durations and charges."""
        return b.models(Session, self._send("GET", f"/v1/calls/{b.seg(call_id)}/sessions"), "sessions")

    # invites

    def create_invite(
        self,
        call_id: str,
        *,
        label: str | None = None,
        expires_at: datetime | None = None,
        max_uses: int | None = None,
    ) -> Invite:
        """``POST /v1/calls/{id}/invites``. The returned ``code`` is shown only now."""
        data = b.body(label=label, expires_at=expires_at, max_uses=max_uses)
        return b.model(Invite, self._send("POST", f"/v1/calls/{b.seg(call_id)}/invites", json=data))

    def list_invites(self, call_id: str) -> list[Invite]:
        """``GET /v1/calls/{id}/invites`` (without codes)."""
        return b.models(Invite, self._send("GET", f"/v1/calls/{b.seg(call_id)}/invites"), "invites")

    def revoke_invite(self, invite_id: str) -> None:
        """``DELETE /v1/invites/{invite_id}``."""
        self._send("DELETE", f"/v1/invites/{b.seg(invite_id)}")

    def preview_invite(self, code: str) -> InvitePreview:
        """``GET /v1/invites/{code}``: call summary, ``can_join_now`` and ``reason`` for the current user."""
        return b.model(InvitePreview, self._send("GET", f"/v1/invites/{b.seg(code)}"))

    # joining

    def join_invite(
        self,
        code: str,
        *,
        user: JoinUser | Mapping[str, Any] | None = None,
        platform: str | None = None,
        forwarded_for: str | None = None,
        user_agent: str | None = None,
    ) -> JoinResponse:
        """``POST /v1/invites/{code}/join``.

        With a user token, joins as that user. With a service token, pass ``user`` (``user_id`` and the end user's
        ``ip`` are required) to join on their behalf; Tele records that IP. ``platform`` is sent as
        ``X-Client-Platform``. ``forwarded_for`` and ``user_agent`` are sent as headers; hosted Tele ignores
        ``X-Forwarded-For`` from customer backends, so prefer ``user=`` with a service token.
        """
        h = b.join_headers(platform, forwarded_for, user_agent)
        data = {"user": b.join_user_input(user)} if user is not None else None
        r = self._send("POST", f"/v1/invites/{b.seg(code)}/join", json=data, headers=h)
        return b.model(JoinResponse, r)

    def join_call(
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
        return b.model(JoinResponse, self._send("POST", f"/v1/calls/{b.seg(call_id)}/join", json=data, headers=h))

    # chat

    def list_messages(self, call_id: str, *, after: str | None = None, limit: int | None = None) -> list[ChatMessage]:
        """``GET /v1/calls/{id}/messages``. Page with ``after=<last message id>``; ``limit`` ≤ 500."""
        q = b.params(after=after, limit=b.limit(limit, 500))
        return b.models(ChatMessage, self._send("GET", f"/v1/calls/{b.seg(call_id)}/messages", params=q), "messages")

    def post_message(
        self, call_id: str, stream_id: str, body: str, *, sent_at: datetime | None = None
    ) -> PostedMessage:
        """``POST /v1/calls/{id}/messages`` (user token). Deduplicated by ``stream_id``."""
        data = b.body(stream_id=stream_id, body=body, sent_at=sent_at)
        return b.model(PostedMessage, self._send("POST", f"/v1/calls/{b.seg(call_id)}/messages", json=data))

    # recordings

    def start_recording(self, call_id: str) -> Recording:
        """``POST /v1/calls/{id}/recording/start``: record a live call."""
        return b.model(Recording, self._send("POST", f"/v1/calls/{b.seg(call_id)}/recording/start"))

    def stop_recording(self, call_id: str) -> Recording:
        """``POST /v1/calls/{id}/recording/stop``."""
        return b.model(Recording, self._send("POST", f"/v1/calls/{b.seg(call_id)}/recording/stop"))

    def list_recordings(self, call_id: str) -> list[Recording]:
        """``GET /v1/calls/{id}/recordings``."""
        return b.models(Recording, self._send("GET", f"/v1/calls/{b.seg(call_id)}/recordings"), "recordings")

    def get_recording(self, recording_id: str) -> Recording:
        """``GET /v1/recordings/{id}``."""
        return b.model(Recording, self._send("GET", f"/v1/recordings/{b.seg(recording_id)}"))

    def restore_recording(self, recording_id: str) -> Recording:
        """``POST /v1/recordings/{id}/restore``: thaw an archived recording (downloadable for 7 days after)."""
        return b.model(Recording, self._send("POST", f"/v1/recordings/{b.seg(recording_id)}/restore"))

    # diagnostics

    def report_diagnostic(self, call_id: str, report: Mapping[str, Any]) -> None:
        """``POST /v1/calls/{id}/diagnostics`` (user token): forward a ``<tele-call>`` diagnostic body as is."""
        self._send("POST", f"/v1/calls/{b.seg(call_id)}/diagnostics", json=dict(report))

    # webhooks

    def create_webhook(self, url: str, events: Sequence[str], *, description: str | None = None) -> WebhookEndpoint:
        """``POST /v1/webhooks``. The returned ``secret`` is shown only now. ``events=["*"]`` for everything."""
        data = b.body(url=url, events=list(events), description=description)
        return b.model(WebhookEndpoint, self._send("POST", "/v1/webhooks", json=data))

    def list_webhooks(self) -> list[WebhookEndpoint]:
        """``GET /v1/webhooks``."""
        return b.models(WebhookEndpoint, self._send("GET", "/v1/webhooks"), "webhooks")

    def get_webhook(self, webhook_id: str) -> WebhookEndpoint:
        """``GET /v1/webhooks/{id}``, including ``failing_since``."""
        return b.model(WebhookEndpoint, self._send("GET", f"/v1/webhooks/{b.seg(webhook_id)}"))

    def update_webhook(
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
        return b.model(WebhookEndpoint, self._send("PATCH", f"/v1/webhooks/{b.seg(webhook_id)}", json=data))

    def delete_webhook(self, webhook_id: str) -> None:
        """``DELETE /v1/webhooks/{id}``: remove the endpoint and its delivery log."""
        self._send("DELETE", f"/v1/webhooks/{b.seg(webhook_id)}")

    def rotate_webhook_secret(self, webhook_id: str) -> RotatedWebhookSecret:
        """``POST /v1/webhooks/{id}/rotate-secret``: a new secret; the old one keeps signing for 24 hours."""
        return b.model(RotatedWebhookSecret, self._send("POST", f"/v1/webhooks/{b.seg(webhook_id)}/rotate-secret"))

    def test_webhook(self, webhook_id: str) -> WebhookTestResult:
        """``POST /v1/webhooks/{id}/test``: queue a ``webhook.test`` event."""
        return b.model(WebhookTestResult, self._send("POST", f"/v1/webhooks/{b.seg(webhook_id)}/test"))

    def list_webhook_deliveries(
        self, webhook_id: str, *, status: str | None = None, limit: int | None = None
    ) -> list[WebhookDelivery]:
        """``GET /v1/webhooks/{id}/deliveries``. ``status``: ``pending``, ``delivered`` or ``failed``.

        ``limit`` is 1-200.
        """
        q = b.params(status=status, limit=b.limit(limit, 200))
        r = self._send("GET", f"/v1/webhooks/{b.seg(webhook_id)}/deliveries", params=q)
        return b.models(WebhookDelivery, r, "deliveries")

    def retry_webhook_delivery(self, delivery_id: str) -> WebhookDeliveryRetry:
        """``POST /v1/webhook-deliveries/{id}/retry``: send a delivery again now."""
        return b.model(WebhookDeliveryRetry, self._send("POST", f"/v1/webhook-deliveries/{b.seg(delivery_id)}/retry"))

    # tenant API keys (admin)

    def create_client(self, name: str, owner_ref: str, *, invite_url_template: str | None = None) -> APIClient:
        """``POST /v1/clients``: a new API key for one of your tenants. ``api_secret`` is shown only now."""
        data = b.body(name=name, owner_ref=owner_ref, invite_url_template=invite_url_template)
        return b.model(APIClient, self._send("POST", "/v1/clients", json=data))

    def list_clients(self, owner_ref: str) -> list[APIClient]:
        """``GET /v1/clients?owner_ref=``: one tenant's keys, revoked ones too."""
        return b.models(APIClient, self._send("GET", "/v1/clients", params=b.params(owner_ref=owner_ref)), "clients")

    def revoke_client(self, client_id: str) -> None:
        """``DELETE /v1/clients/{id}``: revoke a tenant key at once. Its calls and usage stay."""
        self._send("DELETE", f"/v1/clients/{b.seg(client_id)}")

    def rotate_client_secret(self, client_id: str) -> APIClient:
        """``POST /v1/clients/{id}/rotate-secret``: the old secret stops working at once."""
        return b.model(APIClient, self._send("POST", f"/v1/clients/{b.seg(client_id)}/rotate-secret"))

    # usage and rates

    def usage(
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
        return b.model(UsageReport, self._send("GET", "/v1/usage", params=q))

    def usage_csv(
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
        resp = self._send("GET", "/v1/usage", params=q, headers={"Accept": "text/csv"})
        return resp.text

    def list_rates(self, *, active: bool = False) -> list[Rate]:
        """``GET /v1/rates`` (admin). ``active=True`` for the rates in effect now."""
        q = b.params(active=True) if active else None
        return b.models(Rate, self._send("GET", "/v1/rates", params=q), "rates")

    def set_rate(
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
        return b.model(Rate, self._send("POST", "/v1/rates", json=data))

    def end_rate(self, rate_id: str, *, effective_to: datetime | None = None) -> Rate:
        """``PATCH /v1/rates/{id}`` (admin): end a key or team override now or at ``effective_to``."""
        return b.model(Rate, self._send("PATCH", f"/v1/rates/{b.seg(rate_id)}", json=b.body(effective_to=effective_to)))

    # ---- end endpoints ----
