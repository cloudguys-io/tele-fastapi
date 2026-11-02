"""Every endpoint wrapper, sync and async, against a mocked transport: method, path, query, body and parsing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tele_fastapi import AsyncTeleClient, CallUser, InviteOptions, JoinUser, TeleClient

from . import payloads as P

WHEN = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
NO_BODY = object()


@dataclass
class Case:
    name: str
    call: Callable[[Any], Any]
    method: str
    path: str
    response: Any = None
    status: int = 200
    query: dict[str, str] = field(default_factory=dict)
    body: Any = NO_BODY
    headers: dict[str, str] = field(default_factory=dict)
    check: Callable[[Any], Any] = lambda r: None
    auth: bool = True
    text: str | None = None


def _is_none(r: Any) -> None:
    assert r is None


CASES = [
    Case(
        "health",
        lambda c: c.health(),
        "GET",
        "/healthz",
        {"status": "ok"},
        auth=False,
        check=lambda r: r == {"status": "ok"},
    ),
    Case("ready", lambda c: c.ready(), "GET", "/readyz", {"status": "ready"}, auth=False),
    Case(
        "me",
        lambda c: c.me(),
        "GET",
        "/v1/me",
        {"client": "acme", "scope": "user", "user": {"user_id": "u-1", "name": "R", "team": "sales"}},
        check=lambda r: (r.scope == "user" and r.user.team == "sales") or pytest.fail("me"),
    ),
    Case(
        "create_call (service, every field)",
        lambda c: c.create_call(
            "Design review",
            host=CallUser(user_id="agent-7", name="Sumi", team="support"),
            members=[{"user_id": "cust-1", "name": "Customer"}],
            access_policy="any_client_user",
            scheduled_start=WHEN,
            scheduled_end=WHEN.replace(hour=11),
            join_opens_before_minutes=5,
            join_closes_after_minutes=15,
            max_participants=2,
            max_duration_minutes=60,
            record=True,
            invite=InviteOptions(label="cal", max_uses=3, expires_at=WHEN),
        ),
        "POST",
        "/v1/calls",
        P.CALL_CREATED,
        status=201,
        body={
            "title": "Design review",
            "access_policy": "any_client_user",
            "scheduled_start": "2026-10-20T10:00:00+00:00",
            "scheduled_end": "2026-10-20T11:00:00+00:00",
            "join_opens_before_minutes": 5,
            "join_closes_after_minutes": 15,
            "max_participants": 2,
            "max_duration_minutes": 60,
            "record": True,
            "host": {"user_id": "agent-7", "name": "Sumi", "team": "support"},
            "members": [{"user_id": "cust-1", "name": "Customer"}],
            "invite": {"label": "cal", "expires_at": "2026-10-20T10:00:00+00:00", "max_uses": 3},
        },
        check=lambda r: (r.invite.code == "q3V9x2" and r.members[0].role == "host") or pytest.fail("create"),
    ),
    Case(
        "create_call (minimal)",
        lambda c: c.create_call("Instant"),
        "POST",
        "/v1/calls",
        P.CALL_CREATED,
        status=201,
        body={"title": "Instant"},
    ),
    Case(
        "list_calls",
        lambda c: c.list_calls(status=["live", "scheduled"], from_=WHEN, to=WHEN, limit=5),
        "GET",
        "/v1/calls",
        {"calls": [P.CALL]},
        query={
            "status": "live,scheduled",
            "from": "2026-10-20T10:00:00+00:00",
            "to": "2026-10-20T10:00:00+00:00",
            "limit": "5",
        },
        check=lambda r: (len(r) == 1 and r[0].join_opens_at.microsecond == 123456) or pytest.fail("list"),
    ),
    Case(
        "get_call",
        lambda c: c.get_call("call-1"),
        "GET",
        "/v1/calls/call-1",
        P.CALL_WITH_MEMBERS,
        check=lambda r: (r.members[0].user_id == "u-1" and r.invite is None) or pytest.fail("get"),
    ),
    Case(
        "update_call",
        lambda c: c.update_call("call-1", title="New", record=False, max_participants=4),
        "PATCH",
        "/v1/calls/call-1",
        P.CALL,
        body={"title": "New", "max_participants": 4, "record": False},
    ),
    Case(
        "cancel_call",
        lambda c: c.cancel_call("call-1"),
        "DELETE",
        "/v1/calls/call-1",
        {**P.CALL, "status": "cancelled", "cancelled_at": P.T},
        check=lambda r: (r.status == "cancelled" and r.cancelled_at is not None) or pytest.fail("cancel"),
    ),
    Case(
        "end_call",
        lambda c: c.end_call("call-1"),
        "POST",
        "/v1/calls/call-1/end",
        {**P.CALL, "status": "ended", "ended_at": P.T},
    ),
    Case(
        "put_member",
        lambda c: c.put_member("call-1", "user/2", name="Nadia", team="ops"),
        "PUT",
        "/v1/calls/call-1/members/user%2F2",
        {"members": [P.MEMBER]},
        body={"name": "Nadia", "team": "ops"},
        check=lambda r: r[0].name == "Rahim" or pytest.fail("member"),
    ),
    Case(
        "remove_member",
        lambda c: c.remove_member("call-1", "u-2"),
        "DELETE",
        "/v1/calls/call-1/members/u-2",
        status=204,
        check=_is_none,
    ),
    Case(
        "list_sessions",
        lambda c: c.list_sessions("call-1"),
        "GET",
        "/v1/calls/call-1/sessions",
        {"sessions": [P.SESSION]},
        check=lambda r: (
            (r[0].amount == Decimal("11.0000") and r[0].participants[0].ip is None) or pytest.fail("sessions")
        ),
    ),
    Case(
        "create_invite",
        lambda c: c.create_invite("call-1", label="auditor", max_uses=1, expires_at=WHEN),
        "POST",
        "/v1/calls/call-1/invites",
        P.INVITE,
        status=201,
        body={"label": "auditor", "expires_at": "2026-10-20T10:00:00+00:00", "max_uses": 1},
    ),
    Case(
        "list_invites",
        lambda c: c.list_invites("call-1"),
        "GET",
        "/v1/calls/call-1/invites",
        {"invites": [{**P.INVITE, "code": None, "url": None}]},
        check=lambda r: r[0].code is None or pytest.fail("invites"),
    ),
    Case(
        "revoke_invite", lambda c: c.revoke_invite("inv-1"), "DELETE", "/v1/invites/inv-1", status=204, check=_is_none
    ),
    Case(
        "preview_invite",
        lambda c: c.preview_invite("q3V9x2"),
        "GET",
        "/v1/invites/q3V9x2",
        P.PREVIEW,
        check=lambda r: (
            (not r.can_join_now and r.reason.code == "too_early" and r.reason.opens_at is not None)
            or pytest.fail("preview")
        ),
    ),
    Case(
        "join_invite",
        lambda c: c.join_invite("q3V9x2", platform=" Android ", forwarded_for="203.0.113.5", user_agent="App/1"),
        "POST",
        "/v1/invites/q3V9x2/join",
        P.JOIN,
        headers={"x-client-platform": "android", "x-forwarded-for": "203.0.113.5", "user-agent": "App/1"},
        check=lambda r: (r.ice_servers[1].credential == "c" and r.call.status == "live") or pytest.fail("join"),
    ),
    Case(
        "join_call (user token)",
        lambda c: c.join_call("call-1", platform="ios"),
        "POST",
        "/v1/calls/call-1/join",
        {**P.JOIN, "ice_servers": None},
        headers={"x-client-platform": "ios"},
        check=lambda r: r.ice_servers is None or pytest.fail("ice"),
    ),
    Case(
        "join_call (service token)",
        lambda c: c.join_call(
            "call-1", user=JoinUser(user_id="cust-9", ip="203.0.113.50", user_agent="App/3"), platform="flutter"
        ),
        "POST",
        "/v1/calls/call-1/join",
        P.JOIN,
        body={"user": {"user_id": "cust-9", "ip": "203.0.113.50", "user_agent": "App/3"}},
        headers={"x-client-platform": "flutter"},
    ),
    Case(
        "list_messages",
        lambda c: c.list_messages("call-1", after="m-0", limit=500),
        "GET",
        "/v1/calls/call-1/messages",
        {"messages": [P.MESSAGE]},
        query={"after": "m-0", "limit": "500"},
        check=lambda r: r[0].body == "হ্যালো 👋" or pytest.fail("messages"),
    ),
    Case(
        "post_message",
        lambda c: c.post_message("call-1", "TS_1", "hi", sent_at=WHEN),
        "POST",
        "/v1/calls/call-1/messages",
        {"stream_id": "TS_1", "created": True},
        status=201,
        body={"stream_id": "TS_1", "body": "hi", "sent_at": "2026-10-20T10:00:00+00:00"},
        check=lambda r: r.created or pytest.fail("posted"),
    ),
    Case(
        "start_recording",
        lambda c: c.start_recording("call-1"),
        "POST",
        "/v1/calls/call-1/recording/start",
        {**P.RECORDING, "state": "recording"},
        status=202,
    ),
    Case(
        "stop_recording",
        lambda c: c.stop_recording("call-1"),
        "POST",
        "/v1/calls/call-1/recording/stop",
        {**P.RECORDING, "state": "processing"},
        status=202,
    ),
    Case(
        "list_recordings",
        lambda c: c.list_recordings("call-1"),
        "GET",
        "/v1/calls/call-1/recordings",
        {"recordings": [P.RECORDING]},
        check=lambda r: r[0].size_bytes == 142576 or pytest.fail("recs"),
    ),
    Case("get_recording", lambda c: c.get_recording("rec-1"), "GET", "/v1/recordings/rec-1", P.RECORDING),
    Case(
        "restore_recording",
        lambda c: c.restore_recording("rec-1"),
        "POST",
        "/v1/recordings/rec-1/restore",
        {**P.RECORDING, "state": "restoring"},
        status=202,
    ),
    Case(
        "report_diagnostic",
        lambda c: c.report_diagnostic("call-1", {"kind": "connected", "grant_id": "g"}),
        "POST",
        "/v1/calls/call-1/diagnostics",
        status=204,
        body={"kind": "connected", "grant_id": "g"},
        check=_is_none,
    ),
    Case(
        "create_webhook",
        lambda c: c.create_webhook("https://example.com/h", ("call.ended",), description="b"),
        "POST",
        "/v1/webhooks",
        {**P.WEBHOOK, "secret": "whsec_x"},
        status=201,
        body={"url": "https://example.com/h", "events": ["call.ended"], "description": "b"},
        check=lambda r: r.secret == "whsec_x" or pytest.fail("secret"),
    ),
    Case("list_webhooks", lambda c: c.list_webhooks(), "GET", "/v1/webhooks", {"webhooks": [P.WEBHOOK]}),
    Case(
        "get_webhook",
        lambda c: c.get_webhook("wh-1"),
        "GET",
        "/v1/webhooks/wh-1",
        {**P.WEBHOOK, "failing_since": P.T},
        check=lambda r: r.failing_since is not None or pytest.fail("fail"),
    ),
    Case(
        "update_webhook",
        lambda c: c.update_webhook("wh-1", events=["*"], enabled=True),
        "PATCH",
        "/v1/webhooks/wh-1",
        P.WEBHOOK,
        body={"events": ["*"], "enabled": True},
    ),
    Case("delete_webhook", lambda c: c.delete_webhook("wh-1"), "DELETE", "/v1/webhooks/wh-1", status=204),
    Case(
        "rotate_webhook_secret",
        lambda c: c.rotate_webhook_secret("wh-1"),
        "POST",
        "/v1/webhooks/wh-1/rotate-secret",
        {"secret": "whsec_new", "previous_secret_valid_until": P.T},
    ),
    Case(
        "test_webhook",
        lambda c: c.test_webhook("wh-1"),
        "POST",
        "/v1/webhooks/wh-1/test",
        {"event_id": "e-1"},
        status=202,
        check=lambda r: r.event_id == "e-1" or pytest.fail("test"),
    ),
    Case(
        "list_webhook_deliveries",
        lambda c: c.list_webhook_deliveries("wh-1", status="failed", limit=10),
        "GET",
        "/v1/webhooks/wh-1/deliveries",
        {"deliveries": [P.DELIVERY]},
        query={"status": "failed", "limit": "10"},
    ),
    Case(
        "retry_webhook_delivery",
        lambda c: c.retry_webhook_delivery("d-1"),
        "POST",
        "/v1/webhook-deliveries/d-1/retry",
        {"id": "d-1", "status": "pending"},
        status=202,
    ),
    Case(
        "create_client",
        lambda c: c.create_client("Tenant", "tenant-1", invite_url_template="https://t/{code}"),
        "POST",
        "/v1/clients",
        {**P.CLIENT, "api_secret": "s3cret"},
        status=201,
        body={"name": "Tenant", "owner_ref": "tenant-1", "invite_url_template": "https://t/{code}"},
        check=lambda r: r.api_secret == "s3cret" or pytest.fail("client"),
    ),
    Case(
        "list_clients",
        lambda c: c.list_clients("tenant-1"),
        "GET",
        "/v1/clients",
        {"clients": [P.CLIENT]},
        query={"owner_ref": "tenant-1"},
    ),
    Case("revoke_client", lambda c: c.revoke_client("cl-1"), "DELETE", "/v1/clients/cl-1", status=204),
    Case(
        "rotate_client_secret",
        lambda c: c.rotate_client_secret("cl-1"),
        "POST",
        "/v1/clients/cl-1/rotate-secret",
        {**P.CLIENT, "api_secret": "new"},
    ),
    Case(
        "usage",
        lambda c: c.usage(from_=date(2026, 10, 1), to="2026-11-01", group_by="team", team="sales", api_key="TK1"),
        "GET",
        "/v1/usage",
        P.USAGE,
        query={"from": "2026-10-01", "to": "2026-11-01", "group_by": "team", "team": "sales", "api_key": "TK1"},
        check=lambda r: (r.rows[0].room_minutes == Decimal("877") and r.from_.month == 10) or pytest.fail("usage"),
    ),
    Case(
        "usage_csv",
        lambda c: c.usage_csv(group_by="month"),
        "GET",
        "/v1/usage",
        text="sessions,amount\n1,2\n",
        query={"group_by": "month", "format": "csv"},
        headers={"accept": "text/csv"},
        check=lambda r: r.startswith("sessions,") or pytest.fail("csv"),
    ),
    Case(
        "list_rates",
        lambda c: c.list_rates(active=True),
        "GET",
        "/v1/rates",
        {"rates": [P.RATE]},
        query={"active": "true"},
    ),
    Case(
        "set_rate",
        lambda c: c.set_rate("tenant rate", "1.5", "BDT", api_key="TK1", rounding="per_second", min_participants=2),
        "POST",
        "/v1/rates",
        P.RATE,
        status=201,
        body={
            "name": "tenant rate",
            "room_minute_price": "1.5",
            "currency": "BDT",
            "api_key": "TK1",
            "rounding": "per_second",
            "min_participants": 2,
        },
    ),
    Case(
        "end_rate",
        lambda c: c.end_rate("r-1", effective_to=WHEN),
        "PATCH",
        "/v1/rates/r-1",
        {**P.RATE, "effective_to": P.T},
        body={"effective_to": "2026-10-20T10:00:00+00:00"},
    ),
]


def _handler(case: Case, seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if case.text is not None:
            return httpx.Response(case.status, text=case.text, headers={"content-type": "text/csv"})
        if case.response is None:
            return httpx.Response(case.status)
        return httpx.Response(case.status, json=case.response)

    return handle


def _assert_request(case: Case, request: httpx.Request) -> None:
    assert request.method == case.method
    assert request.url.raw_path.split(b"?")[0].decode() == case.path
    assert dict(request.url.params) == case.query
    if case.auth:
        assert request.headers["authorization"] == "Bearer tok"
    else:
        assert "authorization" not in request.headers
    for k, v in case.headers.items():
        assert request.headers[k] == v
    if case.body is NO_BODY:
        assert request.content == b""
    else:
        assert json.loads(request.content) == case.body


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_sync(case: Case) -> None:
    seen: list[httpx.Request] = []
    http = httpx.Client(transport=httpx.MockTransport(_handler(case, seen)))
    with TeleClient(token="tok", base_url="https://tele.test/", http_client=http) as tele:
        result = case.call(tele)
    assert len(seen) == 1
    _assert_request(case, seen[0])
    assert case.check(result) is not False


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_async(case: Case) -> None:
    seen: list[httpx.Request] = []

    async def run() -> Any:
        http = httpx.AsyncClient(transport=httpx.MockTransport(_handler(case, seen)))
        async with AsyncTeleClient(token="tok", base_url="https://tele.test", http_client=http) as tele:
            return await case.call(tele)

    result = asyncio.run(run())
    assert len(seen) == 1
    _assert_request(case, seen[0])
    assert case.check(result) is not False


def test_every_public_endpoint_is_covered() -> None:
    names = {c.name.split(" ")[0] for c in CASES}
    skip = {
        "raw_request",
        "stream_recording",
        "download_recording",
        "as_user",
        "with_token",
        "for_tenant",
        "token",
        "close",
    }
    public = {n for n in dir(TeleClient) if not n.startswith("_") and callable(getattr(TeleClient, n))} - skip
    assert public <= names, public - names
