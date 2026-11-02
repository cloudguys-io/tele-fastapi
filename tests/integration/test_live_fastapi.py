"""The FastAPI routers (via examples/app.py) against the live Tele API."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import jwt
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from tele_fastapi import AsyncTeleClient, TeleClient, TeleSigner, TeleUser
from tele_fastapi.fastapi import tele_join_router, tele_lifespan

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "app.py"


def load_example() -> Any:
    spec = importlib.util.spec_from_file_location("tele_example_live", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_example_app_join_proxy_live(signer: TeleSigner, base_url: str, uid: str) -> None:
    example = load_example()
    app = example.create_app(signer, base_url=base_url)
    host, member = f"{uid}-host", f"{uid}-member"
    with TestClient(app, client=("203.0.113.44", 50000)) as client:
        r = client.post("/calls", json={"title": f"{uid} proxy", "members": [member]}, headers={"X-Demo-User": host})
        assert r.status_code == 201, r.text
        call = r.json()

        # the call UI's getJoin(): invite join and direct join, both returning Tele's join response unchanged
        joined = client.post(
            f"/tele/invites/{call['invite_code']}/join", headers={"X-Demo-User": member, "X-Client-Platform": "android"}
        )
        assert joined.status_code == 200, joined.text
        body = joined.json()
        assert set(body) >= {
            "ws_url",
            "token",
            "token_expires_at",
            "ice_servers",
            "identity",
            "grant_id",
            "role",
            "call",
        }
        assert body["role"] == "participant" and body["call"]["id"] == call["id"]
        hosted = client.post(f"/tele/calls/{call['id']}/join", headers={"X-Demo-User": host})
        assert hosted.status_code == 200 and hosted.json()["role"] == "host"

        # Tele errors come back unchanged too
        stranger = client.post(f"/tele/invites/{call['invite_code']}/join", headers={"X-Demo-User": f"{uid}-x"})
        assert stranger.status_code == 403 and stranger.json()["error"]["code"] == "not_invited"
        assert client.post("/tele/invites/nope/join", headers={"X-Demo-User": member}).json()["error"]["code"] == (
            "invite_not_found"
        )
        assert client.post(f"/tele/calls/{call['id']}/join").status_code == 401

        # diagnostics are relayed: Tele answers session_pending until media reports the participant
        diag = client.post(
            f"/tele/calls/{call['id']}/diagnostics",
            headers={"X-Demo-User": member},
            json={
                "grant_id": body["grant_id"],
                "participant_sid": "PA_proxy",
                "kind": "snapshot",
                "payload": {"state": "connected", "tracks": []},
            },
        )
        assert diag.status_code == 409 and diag.json()["error"]["code"] == "session_pending"

        # the token endpoint mints a user token Tele accepts
        token = client.post("/tele/token", headers={"X-Demo-User": member}).json()["token"]
        assert jwt.decode(token, options={"verify_signature": False})["sub"] == member
        with TeleClient(token=token, base_url=base_url) as as_member:
            me = as_member.me()
            assert me.user is not None and me.user.user_id == member

        assert call["id"] in {c["id"] for c in client.get("/calls", headers={"X-Demo-User": member}).json()}
        assert client.get(f"/calls/{call['id']}/messages", headers={"X-Demo-User": member}).json() == []
        missing = client.get("/recordings/00000000-0000-0000-0000-000000000000/download", headers={"X-Demo-User": host})
        assert missing.status_code == 404
        assert client.post(f"/calls/{call['id']}/end", headers={"X-Demo-User": member}).status_code == 403
        assert client.post(f"/calls/{call['id']}/end", headers={"X-Demo-User": host}).json()["status"] == "ended"


def test_service_join_mode_live(signer: TeleSigner, base_url: str, uid: str) -> None:
    member = TeleUser(f"{uid}-svc-member", "Svc Member", f"{uid}-team")

    def current_user(x_demo_user: str = Header("")) -> TeleUser:
        return member

    app = FastAPI(lifespan=tele_lifespan(lambda: AsyncTeleClient(signer, base_url=base_url)))
    app.include_router(tele_join_router(current_user, service_join=True, client_ip=lambda r: "203.0.113.77"))
    with TeleClient(signer, base_url=base_url) as tele:
        call = tele.create_call(
            f"{uid} service join", host={"user_id": f"{uid}-svc-host"}, members=[{"user_id": member.id}]
        )
        with TestClient(app) as client:
            r = client.post(
                f"/tele/calls/{call.id}/join", headers={"User-Agent": "Huddle/2", "X-Client-Platform": "ios"}
            )
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "participant"
        assert {m.user_id: m.name for m in tele.get_call(call.id).members}[member.id] == "Svc Member"
        tele.cancel_call(call.id)
