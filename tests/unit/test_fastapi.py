from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from tele_fastapi import AsyncTeleClient, TeleSigner, TeleUser
from tele_fastapi.fastapi import (
    TeleClientDep,
    TeleWebhook,
    get_tele,
    tele_join_router,
    tele_lifespan,
    tele_token_router,
)
from tele_fastapi.webhooks import CallEndedEvent, TeleEvent, WebhookTestEvent, sign_webhook

from . import payloads as P

SECRET = "y" * 40
SIGNER = TeleSigner("TK1", SECRET)
ALICE = TeleUser("alice", "Alice", "sales")


def current_user(authorization: str = Header("")) -> TeleUser:
    """Stand-in for the consumer's own authentication."""
    if authorization != "Bearer app-session":
        raise HTTPException(401, "sign in first")
    return ALICE


SIGNED_IN = {"Authorization": "Bearer app-session"}


def claims_of(request: httpx.Request) -> dict[str, Any]:
    out: dict[str, Any] = jwt.decode(request.headers["authorization"][7:], SECRET, algorithms=["HS256"])
    return out


# ---- token router ----


@pytest.mark.parametrize("dep", [current_user, Depends(current_user)])
def test_token_router(dep: Any) -> None:
    app = FastAPI()
    app.include_router(tele_token_router(SIGNER, dep, ttl=600))
    with TestClient(app) as client:
        r = client.post("/tele/token", headers=SIGNED_IN)
        assert r.status_code == 200
        body = r.json()
        claims = jwt.decode(body["token"], SECRET, algorithms=["HS256"])
        assert (claims["sub"], claims["name"], claims["team"], claims["tele"]) == (
            "alice",
            "Alice",
            "sales",
            {"scope": "user"},
        )
        assert claims["exp"] - claims["iat"] == 600
        expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
        assert expires == datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
        assert client.post("/tele/token").status_code == 401


def test_token_router_custom_path() -> None:
    app = FastAPI()
    app.include_router(tele_token_router(SIGNER, current_user, path="/api/tele-token"))
    with TestClient(app) as client:
        assert client.post("/api/tele-token", headers=SIGNED_IN).status_code == 200


# ---- join proxy ----


class FakeTele:
    """Records what reaches 'Tele' and answers like it."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.answer: tuple[int, Any] = (200, P.JOIN)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        status, body = self.answer
        return httpx.Response(status) if body is None else httpx.Response(status, json=body)

    def client(self) -> AsyncTeleClient:
        return AsyncTeleClient(
            SIGNER,
            base_url="https://tele.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )


def proxy_app(fake: FakeTele, **kw: Any) -> FastAPI:
    app = FastAPI(lifespan=tele_lifespan(fake.client))
    app.include_router(tele_join_router(current_user, **kw))
    return app


def test_invite_join_forwards_user_token_ip_platform_and_returns_body_unchanged() -> None:
    fake = FakeTele()
    with TestClient(proxy_app(fake, client_ip=lambda r: "203.0.113.9")) as client:
        r = client.post(
            "/tele/invites/q3V9x2/join",
            headers={**SIGNED_IN, "X-Client-Platform": "Android", "User-Agent": "Huddle/2 (Android 15)"},
        )
    assert r.status_code == 200 and r.json() == P.JOIN
    up = fake.seen[0]
    assert up.method == "POST" and up.url.path == "/v1/invites/q3V9x2/join"
    assert claims_of(up)["sub"] == "alice" and claims_of(up)["tele"] == {"scope": "user"}
    assert up.headers["x-forwarded-for"] == "203.0.113.9"
    assert up.headers["x-client-platform"] == "android"
    assert up.headers["user-agent"] == "Huddle/2 (Android 15)"


def test_call_join_defaults_to_a_service_join() -> None:
    fake = FakeTele()
    with TestClient(proxy_app(fake, client_ip=lambda r: "2001:db8::7")) as client:
        assert client.post("/tele/calls/call-1/join", headers=SIGNED_IN).status_code == 200
    up = fake.seen[0]
    assert up.url.path == "/v1/calls/call-1/join" and up.headers["x-client-platform"] == "web"
    assert claims_of(up)["tele"] == {"scope": "service"}
    assert json.loads(up.content)["user"]["ip"] == "2001:db8::7"


def test_service_join_refuses_a_peer_that_is_not_an_ip() -> None:
    fake = FakeTele()
    with TestClient(proxy_app(fake)) as client:  # TestClient's peer is "testclient" (or absent on old Starlette)
        r = client.post("/tele/calls/call-1/join", headers=SIGNED_IN)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    assert fake.seen == []


def test_call_join_as_user_when_service_join_is_off() -> None:
    fake = FakeTele()
    with TestClient(proxy_app(fake, service_join=False, client_ip=lambda r: "203.0.113.10")) as client:
        assert client.post("/tele/calls/call-1/join", headers=SIGNED_IN).status_code == 200
    up = fake.seen[0]
    assert up.content == b"" and claims_of(up)["sub"] == "alice"
    assert up.headers["x-forwarded-for"] == "203.0.113.10" and up.headers["x-client-platform"] == "web"


@pytest.mark.parametrize(
    ("upstream", "status", "code"),
    [
        ((401, {"error": {"code": "unauthenticated", "message": "bad key"}}), 502, "upstream_auth"),
        ((500, {"error": {"code": "internal", "message": "x"}}), 502, "upstream_error"),
        ((503, None), 502, "upstream_error"),
    ],
)
def test_upstream_auth_and_server_errors_are_mapped(upstream: tuple[int, Any], status: int, code: str) -> None:
    fake = FakeTele()
    fake.answer = upstream
    with TestClient(proxy_app(fake)) as client:
        r = client.post("/tele/invites/abc/join", headers=SIGNED_IN)
    assert r.status_code == status and r.json()["error"]["code"] == code


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (httpx.ConnectError("refused"), 502, "upstream_unreachable"),
        (httpx.ReadTimeout("slow"), 503, "upstream_timeout"),
    ],
)
def test_network_errors_become_json_errors(exc: Exception, status: int, code: str) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc

    tele = AsyncTeleClient(
        SIGNER, base_url="https://tele.test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(boom))
    )
    app = FastAPI()
    app.include_router(tele_join_router(current_user, client=tele, client_ip=lambda r: "203.0.113.1"))
    with TestClient(app) as client:
        for path in ("/tele/invites/abc/join", "/tele/calls/c/join", "/tele/calls/c/diagnostics"):
            r = client.post(path, headers=SIGNED_IN, content=b"{}")
            assert r.status_code == status and r.json()["error"]["code"] == code


@pytest.mark.parametrize(
    "path", ["/tele/invites/%2E%2E/join", "/tele/calls/%2E/join", "/tele/calls/%2E%2E/diagnostics"]
)
def test_dot_segments_are_refused(path: str) -> None:
    fake = FakeTele()
    with TestClient(proxy_app(fake, client_ip=lambda r: "203.0.113.1")) as client:
        r = client.post(path, headers=SIGNED_IN)
    assert r.status_code == 404 and r.json()["error"]["code"] in ("invite_not_found", "not_found")
    assert fake.seen == []


def test_tele_errors_are_relayed_unchanged() -> None:
    fake = FakeTele()
    err = {"error": {"code": "too_early", "message": "not open yet", "opens_at": "2026-10-20T03:50:00Z"}}
    fake.answer = (409, err)
    with TestClient(proxy_app(fake)) as client:
        r = client.post("/tele/invites/q3V9x2/join", headers=SIGNED_IN)
        assert (r.status_code, r.json()) == (409, err)
        assert client.post("/tele/invites/q3V9x2/join").status_code == 401  # our auth runs first
    assert len(fake.seen) == 1


def test_service_join_sends_user_and_ip_in_the_body() -> None:
    fake = FakeTele()
    with TestClient(proxy_app(fake, service_join=True, client_ip=lambda r: "198.51.100.4")) as client:
        r = client.post("/tele/calls/call-1/join", headers={**SIGNED_IN, "User-Agent": "UA/1"})
    assert r.status_code == 200
    up = fake.seen[0]
    assert claims_of(up)["tele"] == {"scope": "service"}
    assert json.loads(up.content) == {
        "user": {"user_id": "alice", "ip": "198.51.100.4", "name": "Alice", "team": "sales", "user_agent": "UA/1"}
    }
    assert "x-forwarded-for" not in up.headers


def test_service_join_without_ip_is_refused() -> None:
    fake = FakeTele()
    with TestClient(proxy_app(fake, service_join=True, client_ip=lambda r: None)) as client:
        assert client.post("/tele/calls/call-1/join", headers=SIGNED_IN).status_code == 400
    assert fake.seen == []


def test_diagnostics_are_forwarded_as_is() -> None:
    fake = FakeTele()
    fake.answer = (204, None)
    report = {"grant_id": "g", "participant_sid": "PA_1", "kind": "connected", "payload": {"tracks": []}}
    with TestClient(proxy_app(fake)) as client:
        r = client.post("/tele/calls/call-1/diagnostics", headers=SIGNED_IN, content=json.dumps(report))
        assert r.status_code == 204
        big = client.post("/tele/calls/call-1/diagnostics", headers=SIGNED_IN, content=b"x" * 70000)
        assert big.status_code == 413 and big.json()["error"]["code"] == "invalid_body"

        def chunks() -> Any:  # no Content-Length: the cap applies while streaming
            for _ in range(20):
                yield b"x" * 4096

        streamed = client.post("/tele/calls/call-1/diagnostics", headers=SIGNED_IN, content=chunks())
        assert streamed.status_code == 413
    up = fake.seen[0]
    assert up.url.path == "/v1/calls/call-1/diagnostics" and json.loads(up.content) == report
    assert claims_of(up)["sub"] == "alice"


def test_diagnostics_route_can_be_disabled_and_client_passed_explicitly() -> None:
    fake = FakeTele()
    app = FastAPI()
    app.include_router(
        tele_join_router(
            current_user, client=fake.client(), prefix="/api", diagnostics=False, client_ip=lambda r: "203.0.113.2"
        )
    )
    with TestClient(app) as client:
        assert client.post("/api/calls/c/join", headers=SIGNED_IN).status_code == 200
        assert client.post("/api/calls/c/diagnostics", headers=SIGNED_IN).status_code == 404


# ---- shared client ----


def test_lifespan_and_dependency() -> None:
    fake = FakeTele()
    fake.answer = (200, {"client": "acme", "scope": "service"})
    app = FastAPI(lifespan=tele_lifespan(fake.client))

    @app.get("/who")
    async def who(tele: TeleClientDep) -> dict[str, str]:
        return {"client": (await tele.me()).client}

    with TestClient(app) as client:
        assert client.get("/who").json() == {"client": "acme"}
        tele = app.state.tele_client
        assert isinstance(tele, AsyncTeleClient)
    assert app.state.tele_client is None
    assert tele._http.is_closed is False  # we passed our own http_client, so it is ours to close


def test_get_tele_without_lifespan_fails_loudly() -> None:
    app = FastAPI()

    @app.get("/x")
    def x(tele: AsyncTeleClient = Depends(get_tele)) -> None:
        return None

    with TestClient(app, raise_server_exceptions=True) as client, pytest.raises(RuntimeError, match="tele_lifespan"):
        client.get("/x")


# ---- webhooks ----

HOOK_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
OTHER_SECRET = "whsec_" + "A" * 43 + "="


def webhook_app(secret: Any) -> tuple[FastAPI, list[TeleEvent]]:
    app = FastAPI()
    got: list[TeleEvent] = []

    @app.post("/tele/webhooks", status_code=204, response_model=None)
    async def hook(event: TeleEvent = Depends(TeleWebhook(secret))) -> None:
        got.append(event)

    return app, got


def signed(body: bytes, secret: str = HOOK_SECRET, msg_id: str = "evt-1") -> dict[str, str]:
    ts = int(time.time())
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sign_webhook(secret, msg_id, ts, body),
        "content-type": "application/json",
    }


ENDED = json.dumps(
    {
        "id": "evt-1",
        "type": "call.ended",
        "api_version": "2026-10-19",
        "occurred_at": "2026-10-19T10:10:35Z",
        "data": {
            "call": {"id": "c", "title": "t", "host_user_id": "h"},
            "session": {
                "id": "s",
                "ended_at": "2026-10-19T10:10:35Z",
                "peak_participants": 1,
                "unique_participants": 1,
            },
        },
    }
).encode()


def test_webhook_dependency_yields_typed_event() -> None:
    app, got = webhook_app(HOOK_SECRET)
    with TestClient(app) as client:
        assert client.post("/tele/webhooks", content=ENDED, headers=signed(ENDED)).status_code == 204
    assert isinstance(got[0], CallEndedEvent) and got[0].data.usage is None


def test_webhook_bad_signature_is_401_and_bad_payload_400() -> None:
    app, got = webhook_app(HOOK_SECRET)
    with TestClient(app) as client:
        bad = client.post("/tele/webhooks", content=ENDED, headers=signed(ENDED, OTHER_SECRET))
        assert bad.status_code == 401
        assert client.post("/tele/webhooks", content=ENDED).status_code == 401
        stale = {**signed(ENDED), "webhook-timestamp": str(int(time.time()) - 1000)}
        assert client.post("/tele/webhooks", content=ENDED, headers=stale).status_code == 401
        junk = b'{"type": "call.ended", "data": {}}'
        assert client.post("/tele/webhooks", content=junk, headers=signed(junk)).status_code == 400
    assert got == []


def test_webhook_rotation_and_callable_secrets() -> None:
    current: list[str] = []
    app, got = webhook_app(lambda: current)
    body = json.dumps(
        {
            "id": "e",
            "type": "webhook.test",
            "api_version": "2026-10-19",
            "occurred_at": "2026-10-19T10:00:00Z",
            "data": {"webhook_id": "wh"},
        }
    ).encode()
    with TestClient(app) as client:
        assert client.post("/tele/webhooks", content=body, headers=signed(body)).status_code == 503
        current[:] = [OTHER_SECRET, HOOK_SECRET]
        assert client.post("/tele/webhooks", content=body, headers=signed(body)).status_code == 204
    assert isinstance(got[0], WebhookTestEvent)
    assert TeleWebhook(lambda: None).secrets() == []


def test_webhook_with_only_malformed_secrets_is_a_500() -> None:
    app, _ = webhook_app(["whsec_***", "nope!"])
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/tele/webhooks", content=ENDED, headers=signed(ENDED))
    assert r.status_code == 500 and "misconfigured" in r.json()["detail"]
    app, _got = webhook_app(["whsec_***", HOOK_SECRET])
    with TestClient(app) as client:
        assert client.post("/tele/webhooks", content=ENDED, headers=signed(ENDED)).status_code == 204
    assert TeleWebhook([HOOK_SECRET, ""]).secrets() == [HOOK_SECRET]
