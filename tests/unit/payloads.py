"""Response bodies shaped exactly like the Tele server's DTOs (server/internal/api/dto.go and friends)."""

from __future__ import annotations

from typing import Any

T = "2026-10-20T04:00:00Z"
T_NANOS = "2026-10-20T04:00:00.123456789Z"  # Go emits nanoseconds

MEMBER = {"user_id": "u-1", "name": "Rahim", "role": "host", "added_at": T}
INVITE = {
    "id": "inv-1",
    "code": "q3V9x2",
    "url": "https://app.example/call/q3V9x2",
    "label": "calendar link",
    "expires_at": None,
    "max_uses": None,
    "use_count": 0,
    "created_at": T,
}
CALL: dict[str, Any] = {
    "id": "call-1",
    "title": "Design review",
    "status": "scheduled",
    "access_policy": "listed_only",
    "host_user_id": "u-1",
    "scheduled_start": T,
    "scheduled_end": None,
    "join_opens_at": T_NANOS,
    "join_closes_at": None,
    "join_opens_before_minutes": 10,
    "join_closes_after_minutes": 30,
    "max_participants": 10,
    "max_duration_minutes": 240,
    "record": False,
    "created_at": T,
}
CALL_CREATED = {**CALL, "members": [MEMBER], "invite": INVITE}
CALL_WITH_MEMBERS = {**CALL, "members": [MEMBER]}
PREVIEW = {
    "call": {
        "id": "call-1",
        "title": "Design review",
        "status": "scheduled",
        "host": {"user_id": "u-1", "name": "Rahim"},
        "scheduled_start": T,
        "scheduled_end": None,
        "join_opens_at": T,
        "join_closes_at": None,
    },
    "invite": {"label": "calendar link", "expires_at": None},
    "can_join_now": False,
    "reason": {"code": "too_early", "message": "the call is not open for joining yet", "opens_at": T},
}
JOIN = {
    "ws_url": "wss://tele-lk.cloudguys.io",
    "token": "lk-token",
    "token_expires_at": T,
    "ice_servers": [
        {"urls": ["stun:stun.cloudflare.com:3478"]},
        {"urls": ["turn:turn.cloudflare.com:3478?transport=udp"], "username": "u", "credential": "c"},
    ],
    "identity": "u:01a1",
    "grant_id": "grant-1",
    "role": "participant",
    "call": {**CALL, "status": "live"},
}
MESSAGE = {"id": "m-1", "stream_id": "TS_1", "user_id": "bob", "name": "Bob", "body": "হ্যালো 👋", "sent_at": T}
RECORDING = {
    "id": "rec-1",
    "call_id": "call-1",
    "state": "available",
    "started_at": T,
    "ended_at": T,
    "duration_seconds": 53,
    "size_bytes": 142576,
}
SESSION = {
    "id": "s-1",
    "room_sid": "RM_1",
    "room_name": "call_call-1",
    "closed_at": T,
    "closed_by": "webhook",
    "status": "billed",
    "started_at": T,
    "ended_at": T,
    "duration_seconds": 605,
    "billable_seconds": 605,
    "billable_minutes": "11.0000",
    "amount": "11.0000",
    "currency": "BDT",
    "peak_participants": 2,
    "unique_participants": 2,
    "end_reason": "ROOM_END_IDLE_TIMEOUT",
    "participants": [
        {
            "id": "p-1",
            "user_internal_id": "x",
            "participant_sid": "PA_1",
            "identity": "u:x",
            "grant_id": None,
            "last_seen_at": T,
            "ip_source": "request",
            "team": None,
            "user_id": "alice",
            "name": "Alice",
            "joined_at": T,
            "left_at": T,
            "duration_seconds": 605,
            "ip": None,
            "user_agent": None,
            "platform": "web",
        }
    ],
}
WEBHOOK = {
    "id": "wh-1",
    "url": "https://example.com/tele-hook",
    "description": "billing",
    "events": ["call.ended"],
    "enabled": True,
    "created_at": T,
    "updated_at": T,
}
DELIVERY = {
    "id": "d-1",
    "event_id": "e-1",
    "type": "webhook.test",
    "status": "pending",
    "attempts": 0,
    "next_attempt_at": T,
    "created_at": T,
}
CLIENT = {
    "id": "cl-1",
    "name": "Tenant",
    "api_key": "TKabc",
    "owner_ref": "tenant-1",
    "invite_url_template": None,
    "created_at": T,
    "revoked_at": None,
}
USAGE = {
    "from": "2026-10-01T00:00:00Z",
    "to": "2026-11-01T00:00:00Z",
    "group_by": "team",
    "rows": [
        {
            "group": {"api_key": "TK3f9", "team": "sales"},
            "sessions": 42,
            "room_seconds": 51310,
            "room_minutes": "877.0000",
            "recording_minutes": "0.0000",
            "adjustments": "0.0000",
            "amount": "877.0000",
            "currency": "BDT",
        }
    ],
}
RATE = {
    "id": "r-1",
    "name": "default",
    "scope": {},
    "room_minute_price": "1.0000",
    "recording_minute_price": "0.0000",
    "currency": "BDT",
    "rounding": "ceil_minute",
    "min_billable_minutes": 0,
    "min_participants": 1,
    "effective_from": T,
    "effective_to": None,
}
