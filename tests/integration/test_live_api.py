"""The whole client against a live Tele API (with LiveKit, so joins mint real media tokens)."""

from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tele_fastapi import (
    AsyncTeleClient,
    AuthenticationError,
    BadRequestError,
    CallUser,
    ConflictError,
    GoneError,
    JoinUser,
    NotFoundError,
    NotImplementedByServerError,
    PermissionDeniedError,
    TeleClient,
    TeleError,
    TeleSigner,
)

from .conftest import person


def test_me(tele: TeleClient, uid: str) -> None:
    me = tele.me()
    assert me.scope == "service" and me.user is None and me.client
    alice = person(uid, "Alice")
    me = tele.as_user(alice).me()
    assert me.scope == "user" and me.user is not None
    assert (me.user.user_id, me.user.name, me.user.team) == (alice.id, "Alice", alice.team)
    assert tele.health() == {"status": "ok"} and tele.ready() == {"status": "ready"}
    with pytest.raises(AuthenticationError) as info:
        tele.with_token("not-a-jwt").me()
    assert info.value.code == "unauthenticated"


def test_call_lifecycle(tele: TeleClient, uid: str) -> None:
    host, member, guest, outsider = (person(uid, n) for n in ("Host", "Member", "Guest", "Outsider"))

    # create -> invite (service token, the host is explicit)
    call = tele.create_call(
        f"{uid} lifecycle",
        host=CallUser(user_id=host.id, name=host.name, team=host.team),
        members=[CallUser(user_id=member.id, name=member.name)],
        max_participants=4,
        invite={"label": "default", "max_uses": 5},
    )
    assert call.status == "scheduled" and call.host_user_id == host.id
    assert {m.user_id: m.role for m in call.members} == {host.id: "host", member.id: "participant"}
    assert call.invite is not None and call.invite.code and call.invite.max_uses == 5
    code = call.invite.code

    # preview as the member, as an outsider (listed_only) and as the backend
    as_member = tele.as_user(member)
    preview = as_member.preview_invite(code)
    assert preview.can_join_now and preview.reason is None
    assert preview.call.id == call.id and preview.call.host.user_id == host.id
    denied = tele.as_user(outsider).preview_invite(code)
    assert not denied.can_join_now and denied.reason is not None and denied.reason.code == "not_invited"
    assert tele.preview_invite(code).can_join_now

    # join: through the invite (user token), directly (host user token), on a user's behalf (service token)
    joined = as_member.join_invite(code, platform="android", forwarded_for="203.0.113.7", user_agent="it/1")
    assert joined.role == "participant" and joined.ws_url and joined.token and joined.grant_id
    assert joined.call.id == call.id and joined.identity.startswith("u:")
    as_host = tele.as_user(host)
    host_join = as_host.join_call(call.id, platform="web")
    assert host_join.role == "host"
    with pytest.raises(PermissionDeniedError) as info:
        tele.as_user(outsider).join_invite(code)
    assert info.value.code == "not_invited"
    assert tele.put_member(call.id, guest.id, name=guest.name)
    svc_join = tele.join_call(
        call.id, user=JoinUser(user_id=guest.id, ip="198.51.100.20", user_agent="it/2"), platform="flutter"
    )
    assert svc_join.role == "participant"
    with pytest.raises(BadRequestError):
        tele.join_call(call.id)  # a service token must say who is joining

    # chat: post as the member, read as the member and the backend
    stream_id = f"TS_{uuid.uuid4().hex[:12]}"
    posted = as_member.post_message(call.id, stream_id, "হ্যালো 👋 from tele-fastapi")
    assert posted.created and posted.stream_id == stream_id
    assert not as_member.post_message(call.id, stream_id, "again").created
    with pytest.raises(PermissionDeniedError):
        tele.post_message(call.id, "TS_svc", "service tokens can't post")
    history = tele.list_messages(call.id, limit=50)
    assert [(m.stream_id, m.user_id, m.body) for m in history] == [(stream_id, member.id, "হ্যালো 👋 from tele-fastapi")]
    assert as_member.list_messages(call.id, after=history[0].id) == []

    # list, get, patch
    assert call.id in {c.id for c in as_member.list_calls()}
    assert call.id in {c.id for c in tele.list_calls(status=["scheduled", "live"], limit=200)}
    got = as_member.get_call(call.id)
    assert {m.user_id for m in got.members} == {host.id, member.id, guest.id}
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    patched = as_host.update_call(
        call.id,
        title=f"{uid} renamed",
        max_participants=5,
        scheduled_start=datetime.now(timezone.utc),
        scheduled_end=later,
    )
    assert patched.title == f"{uid} renamed" and patched.max_participants == 5
    assert patched.scheduled_end is not None and abs((patched.scheduled_end - later).total_seconds()) < 1
    with pytest.raises(BadRequestError):
        tele.update_call(call.id, max_participants=50)
    with pytest.raises(PermissionDeniedError):
        as_member.update_call(call.id, title="not the host")

    # members
    tele.remove_member(call.id, guest.id)
    assert guest.id not in {m.user_id for m in tele.get_call(call.id).members}
    with pytest.raises(NotFoundError):
        tele.remove_member(call.id, host.id)  # the host can't be removed

    # invites
    extra = as_host.create_invite(call.id, label="auditor", max_uses=1, expires_at=later)
    assert extra.code and extra.label == "auditor"
    listed = tele.list_invites(call.id)
    assert {i.id for i in listed} >= {call.invite.id, extra.id} and all(i.code is None for i in listed)
    tele.revoke_invite(extra.id)
    revoked = as_member.preview_invite(extra.code)
    assert revoked.reason is not None and revoked.reason.code == "invite_revoked"
    with pytest.raises(GoneError) as gone:
        as_member.join_invite(extra.code)
    assert gone.value.code == "invite_revoked" and gone.value.is_invite_invalid
    with pytest.raises(NotFoundError) as nf:
        as_member.preview_invite("no-such-code")
    assert nf.value.code == "invite_not_found"

    # sessions (the room exists once someone joined; participants appear when media connects)
    sessions = tele.list_sessions(call.id)
    assert sessions and sessions[0].status in ("open", "closed", "billed")

    # recordings: nobody is publishing, so the call is not live
    assert tele.list_recordings(call.id) == []
    with pytest.raises((ConflictError, NotImplementedByServerError)) as rec:
        as_host.start_recording(call.id)
    assert rec.value.code in ("call_not_live", "recording_disabled")
    with pytest.raises(ConflictError) as rec:
        tele.stop_recording(call.id)
    assert rec.value.code == "not_recording"
    missing = str(uuid.uuid4())
    for op in (tele.get_recording, tele.restore_recording, tele.download_recording):
        with pytest.raises(NotFoundError):
            op(missing)

    # diagnostics: the grant is real, but no media session reported it yet
    report = {
        "grant_id": joined.grant_id,
        "participant_sid": "PA_tele_fastapi",
        "kind": "connected",
        "payload": {
            "state": "connected",
            "user_agent": "it",
            "timezone": "UTC",
            "locale": "en",
            "viewport": "1x1",
            "microphone_enabled": False,
            "camera_enabled": False,
            "can_playback_audio": True,
            "tracks": [],
        },
    }
    with pytest.raises(ConflictError) as diag:
        as_member.report_diagnostic(call.id, report)
    assert diag.value.code == "session_pending"

    # end
    ended = as_host.end_call(call.id)
    assert ended.status == "ended" and ended.ended_at is not None
    with pytest.raises(ConflictError) as closed:
        tele.update_call(call.id, title="too late")
    assert closed.value.code == "call_closed" and closed.value.is_call_closed
    with pytest.raises(ConflictError) as over:
        as_member.join_invite(code)
    assert over.value.code == "call_ended"


def test_cancel_and_too_early(tele: TeleClient, uid: str) -> None:
    host, member = person(uid, "Host"), person(uid, "Member")
    start = datetime.now(timezone.utc) + timedelta(days=1)
    call = tele.create_call(
        f"{uid} tomorrow",
        host={"user_id": host.id},
        members=[{"user_id": member.id}],
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=45),
        access_policy="listed_only",
        join_opens_before_minutes=5,
    )
    assert call.join_opens_at is not None and call.invite is not None and call.invite.code
    preview = tele.as_user(member).preview_invite(call.invite.code)
    assert preview.reason is not None and preview.reason.code == "too_early" and preview.reason.opens_at
    with pytest.raises(ConflictError) as early:
        tele.as_user(member).join_invite(call.invite.code)
    assert early.value.code == "too_early" and early.value.opens_at == call.join_opens_at
    cancelled = tele.as_user(host).cancel_call(call.id)
    assert cancelled.status == "cancelled" and cancelled.cancelled_at is not None
    with pytest.raises(ConflictError) as again:
        tele.cancel_call(call.id)
    assert again.value.code == "call_closed"


def test_webhooks_crud_rotate_test_deliveries(tele: TeleClient, uid: str) -> None:
    hook = tele.create_webhook(
        f"https://example.com/tele-hook/{uid}", ["call.ended", "recording.ready"], description=f"{uid} hook"
    )
    try:
        assert hook.secret and hook.secret.startswith("whsec_") and hook.enabled
        assert hook.id in {w.id for w in tele.list_webhooks()}
        got = tele.get_webhook(hook.id)
        assert got.secret is None and got.events == ["call.ended", "recording.ready"]

        updated = tele.update_webhook(hook.id, events=["*"], description=f"{uid} all", enabled=False)
        assert updated.events == ["*"] and not updated.enabled and updated.disabled_reason
        assert tele.update_webhook(hook.id, enabled=True).enabled
        with pytest.raises(BadRequestError):
            tele.update_webhook(hook.id, events=["no.such.event"])

        rotated = tele.rotate_webhook_secret(hook.id)
        assert rotated.secret.startswith("whsec_") and rotated.secret != hook.secret
        assert rotated.previous_secret_valid_until > datetime.now(timezone.utc) + timedelta(hours=23)

        sent = tele.test_webhook(hook.id)
        deliveries = tele.list_webhook_deliveries(hook.id, limit=10)
        mine = [d for d in deliveries if d.event_id == sent.event_id]
        assert mine and mine[0].type == "webhook.test"
        assert all(d.status == "pending" for d in tele.list_webhook_deliveries(hook.id, status="pending"))
        retried = tele.retry_webhook_delivery(mine[0].id)
        assert retried.id == mine[0].id and retried.status == "pending"
        with pytest.raises(NotFoundError):
            tele.retry_webhook_delivery(str(uuid.uuid4()))
        with pytest.raises(PermissionDeniedError):
            tele.as_user(person(uid, "Nobody")).list_webhooks()
    finally:
        tele.delete_webhook(hook.id)
    with pytest.raises(NotFoundError):
        tele.get_webhook(hook.id)


def test_usage_json_and_csv(tele: TeleClient) -> None:
    now = datetime.now(timezone.utc)
    report = tele.usage(from_=now.date().replace(day=1), to=now + timedelta(days=1), group_by="team")
    assert report.group_by == "team" and report.from_.day == 1
    for row in report.rows:
        assert row.currency and row.amount >= 0 and "team" in row.group
    by_month = tele.usage(group_by="month")
    assert by_month.group_by == "month"
    text = tele.usage_csv(group_by="call")
    header = next(csv.reader(io.StringIO(text)))
    assert header[-7:] == [
        "sessions",
        "room_seconds",
        "room_minutes",
        "recording_minutes",
        "adjustments",
        "amount",
        "currency",
    ]
    with pytest.raises(BadRequestError):
        tele.usage(group_by="planet")


def test_admin_tenant_keys_tele_client_and_rates(admin: TeleClient, tele: TeleClient, uid: str, base_url: str) -> None:
    owner = f"{uid}-tenant"
    created = admin.create_client(f"{uid} tenant", owner, invite_url_template="https://tenant.example/c/{code}")
    try:
        assert created.api_secret and created.api_key and created.owner_ref == owner
        assert [c.id for c in admin.list_clients(owner)] == [created.id]

        rotated = admin.rotate_client_secret(created.id)
        assert rotated.api_secret and rotated.api_secret != created.api_secret
        tenant = TeleClient(TeleSigner(created.api_key, rotated.api_secret), base_url=base_url)
        assert tenant.me().client == f"{uid} tenant"
        with pytest.raises(AuthenticationError):
            TeleClient(TeleSigner(created.api_key, created.api_secret), base_url=base_url).me()
        tenant_call = tenant.create_call(f"{uid} tenant call", host={"user_id": f"{uid}-th"})
        assert tenant_call.invite is not None and tenant_call.invite.url == (
            f"https://tenant.example/c/{tenant_call.invite.code}"
        )
        tenant.cancel_call(tenant_call.id)

        # an admin key runs the tenant's webhooks with Tele-Client
        as_tenant = admin.for_tenant(created.id)
        hook = as_tenant.create_webhook(f"https://example.com/tele-hook/{uid}-tenant", ["*"])
        assert [w.id for w in tenant.list_webhooks()] == [hook.id]
        assert hook.id not in {w.id for w in admin.list_webhooks()}
        as_tenant.delete_webhook(hook.id)
        assert tenant.list_webhooks() == []
        with pytest.raises(PermissionDeniedError):
            tele.for_tenant(created.id).list_webhooks()  # not an admin key

        # rates: a tenant-scoped override, then end it
        assert admin.list_rates(active=True)
        rate = admin.set_rate(f"{uid} override", "0.5", "BDT", api_key=created.api_key, rounding="per_second")
        assert rate.scope.api_client_id == created.id and rate.room_minute_price == 0.5
        ended = admin.end_rate(rate.id)
        assert ended.effective_to is not None
        assert rate.id not in {r.id for r in admin.list_rates(active=True)}
        assert admin.usage(api_key=created.api_key).rows == []
        with pytest.raises(PermissionDeniedError):
            tele.list_rates()
        tenant.close()
    finally:
        admin.revoke_client(created.id)
    listed = admin.list_clients(owner)
    assert listed[0].revoked_at is not None and listed[0].api_secret is None
    with pytest.raises(ConflictError):
        admin.revoke_client(created.id)


def test_async_client_live(signer: TeleSigner, base_url: str, uid: str) -> None:
    host, member = person(uid, "Host"), person(uid, "Member")

    async def run() -> None:
        async with AsyncTeleClient(signer, base_url=base_url) as tele:
            assert (await tele.me()).scope == "service"
            call = await tele.create_call(
                f"{uid} async", host=CallUser(user_id=host.id), access_policy="any_client_user"
            )
            assert call.invite is not None and call.invite.code
            as_member = tele.as_user(member)
            preview, joined = await asyncio.gather(
                as_member.preview_invite(call.invite.code), tele.as_user(host).join_call(call.id, platform="web")
            )
            assert preview.can_join_now and joined.role == "host"
            via_invite = await as_member.join_invite(call.invite.code, platform="ios")
            assert via_invite.role == "participant"  # any_client_user: joining makes you a member
            assert (await as_member.post_message(call.id, f"TS_{uuid.uuid4().hex[:12]}", "hello")).created
            assert [m.body for m in await tele.list_messages(call.id)] == ["hello"]
            assert await tele.list_sessions(call.id)
            with pytest.raises(TeleError):
                await tele.download_recording(str(uuid.uuid4()))
            assert (await tele.as_user(host).end_call(call.id)).status == "ended"

    asyncio.run(run())


def test_service_invite_join_and_user_ids_with_slashes(tele: TeleClient, uid: str) -> None:
    host = person(uid, "Host")
    slashed = f"{uid}/org-7/user"  # user ids may contain "/": escaped as %2F in member paths
    call = tele.create_call(f"{uid} service invite", host={"user_id": host.id}, max_participants=4)
    assert call.invite is not None and call.invite.code
    try:
        members = tele.put_member(call.id, slashed, name="Slashed")
        assert slashed in {m.user_id for m in members}

        # a service token joins through the invite on the user's behalf, with their IP
        joined = tele.join_invite(
            call.invite.code,
            user=JoinUser(user_id=slashed, ip="198.51.100.31", user_agent="it/svc-invite", name="Slashed"),
            platform="flutter-ios",
        )
        assert joined.role == "participant" and joined.call.id == call.id
        with pytest.raises(BadRequestError):
            tele.join_invite(call.invite.code)  # a service token must say who is joining

        tele.remove_member(call.id, slashed)
        assert slashed not in {m.user_id for m in tele.get_call(call.id).members}

        # list limits outside the server's range are refused before any request
        with pytest.raises(ValueError, match="1 to 200"):
            tele.list_calls(limit=201)
        assert len(tele.list_calls(limit=1)) <= 1
        assert tele.raw_request("GET", "/v1/calls", params={"limit": "201"}).status_code == 400
    finally:
        tele.cancel_call(call.id)
