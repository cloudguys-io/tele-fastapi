from __future__ import annotations

import base64
import json
import os
from decimal import Decimal
from typing import Any

import pytest

from tele_fastapi.webhooks import (
    EVENT_TYPES,
    CallCancelledEvent,
    CallCreatedEvent,
    CallEndedEvent,
    CallStartedEvent,
    CallUpdatedEvent,
    ChatMessageCreatedEvent,
    ParticipantJoinedEvent,
    ParticipantLeftEvent,
    RecordingFailedEvent,
    RecordingReadyEvent,
    RecordingStartedEvent,
    UnknownEvent,
    WebhookPayloadError,
    WebhookTestEvent,
    WebhookVerificationError,
    construct_event,
    parse_event,
    sign_webhook,
    verify_webhook,
)

# The Standard Webhooks reference vector, also used by server/internal/webhooks/webhooks_test.go,
# sdk/go/teletoken/webhook_test.go and sdk/ts/node/src/webhook.test.ts.
SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
MSG_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
TS = 1614265330
BODY = b'{"test": 2432232314}'
SIG = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def headers(sig: str = SIG, ts: int = TS, msg_id: str = MSG_ID) -> dict[str, str]:
    return {"webhook-id": msg_id, "webhook-timestamp": str(ts), "webhook-signature": sig}


def new_secret() -> str:
    return "whsec_" + base64.b64encode(os.urandom(32)).decode()  # like webhooks.NewSecret on the server


def test_sign_matches_reference_vector() -> None:
    assert sign_webhook(SECRET, MSG_ID, TS, BODY) == SIG


def test_verify_returns_id_and_accepts_any_of_several_signatures() -> None:
    assert verify_webhook(SECRET, headers(), BODY, now=TS) == MSG_ID
    assert verify_webhook(SECRET, headers(f"v1,bm90IHRoaXMgb25l {SIG}"), BODY.decode(), now=TS) == MSG_ID


def test_rotation_overlap_two_signatures_either_secret_verifies() -> None:
    old, new = new_secret(), new_secret()
    body = b'{"id":"e","type":"webhook.test"}'
    sigs = f"{sign_webhook(new, 'e', TS, body)} {sign_webhook(old, 'e', TS, body)}"
    h = headers(sigs, msg_id="e")
    assert verify_webhook(old, h, body, now=TS) == "e"
    assert verify_webhook(new, h, body, now=TS) == "e"
    assert verify_webhook([new_secret(), new], h, body, now=TS) == "e"
    with pytest.raises(WebhookVerificationError, match="does not match"):
        verify_webhook(new_secret(), h, body, now=TS)


def test_headers_are_case_insensitive_for_plain_dicts() -> None:
    h = {k.title(): v for k, v in headers().items()}
    assert verify_webhook(SECRET, h, BODY, now=TS) == MSG_ID


@pytest.mark.parametrize(
    ("h", "now", "message"),
    [
        ({}, TS, "missing"),
        (headers(ts=TS), TS + 301, "too old"),
        (headers(ts=TS), TS - 301, "too old"),
        ({**headers(), "webhook-timestamp": "soon"}, TS, "too old"),
        (headers("v1,AAAA"), TS, "does not match"),
    ],
)
def test_verify_rejects(h: dict[str, str], now: int, message: str) -> None:
    with pytest.raises(WebhookVerificationError, match=message):
        verify_webhook(SECRET, h, BODY, now=now)


def test_tolerance_edges_and_tampering() -> None:
    assert verify_webhook(SECRET, headers(), BODY, now=TS + 300)
    with pytest.raises(WebhookVerificationError):
        verify_webhook(SECRET, headers(), BODY + b" ", now=TS)
    with pytest.raises(WebhookVerificationError, match="base64"):
        verify_webhook("whsec_***", headers(), BODY, now=TS)
    with pytest.raises(WebhookVerificationError, match="no webhook secret"):
        verify_webhook([], headers(), BODY, now=TS)


CALL = {"id": "c1", "title": "Weekly", "host_user_id": "user-123"}
FULL_CALL = {
    **CALL,
    "status": "live",
    "scheduled_start": None,
    "scheduled_end": None,
    "record": False,
    "created_at": "2026-10-19T10:00:00Z",
}
PARTICIPANT = {
    "session_id": "s1",
    "participant_sid": "PA_1",
    "user_id": "u1",
    "name": "Alice",
    "platform": "web",
    "joined_at": "2026-10-19T10:00:30Z",
    "left_at": "2026-10-19T10:10:00Z",
    "disconnect_reason": "CLIENT_INITIATED",
}
RECORDING = {
    "id": "r1",
    "status": "complete",
    "started_at": "2026-10-19T10:00:30Z",
    "ended_at": None,
    "duration_ms": 5300,
    "size_bytes": 1000,
    "error": None,
}

DATA: dict[str, tuple[type[Any], dict[str, Any]]] = {
    "call.created": (CallCreatedEvent, {"call": FULL_CALL}),
    "call.updated": (CallUpdatedEvent, {"call": {**FULL_CALL, "status": "ended", "ended_at": "2026-10-19T11:00:00Z"}}),
    "call.cancelled": (CallCancelledEvent, {"call": {**FULL_CALL, "status": "cancelled"}}),
    "call.started": (CallStartedEvent, {"call": CALL, "session_id": "s1", "started_at": "2026-10-19T10:00:30Z"}),
    "call.ended": (
        CallEndedEvent,
        {
            "call": CALL,
            "session": {
                "id": "s1",
                "started_at": "2026-10-19T10:00:30Z",
                "ended_at": "2026-10-19T10:10:35Z",
                "end_reason": "ROOM_END_IDLE_TIMEOUT",
                "billable_seconds": 605,
                "peak_participants": 2,
                "unique_participants": 2,
            },
            "usage": {"metric": "room_minutes", "billable_minutes": "11.0000", "amount": "11.0000", "currency": "BDT"},
        },
    ),
    "participant.joined": (ParticipantJoinedEvent, {"call": CALL, "participant": {**PARTICIPANT, "left_at": None}}),
    "participant.left": (ParticipantLeftEvent, {"call": CALL, "participant": PARTICIPANT}),
    "recording.started": (RecordingStartedEvent, {"call": CALL, "recording": {**RECORDING, "status": "active"}}),
    "recording.ready": (RecordingReadyEvent, {"call": CALL, "recording": RECORDING}),
    "recording.failed": (RecordingFailedEvent, {"call": CALL, "recording": {**RECORDING, "error": "egress"}}),
    "chat.message.created": (
        ChatMessageCreatedEvent,
        {
            "call_id": "c1",
            "message": {"id": "m1", "stream_id": "TS_1", "sender_user_id": "u1", "sent_at": "2026-10-19T10:01:00Z"},
        },
    ),
    "webhook.test": (WebhookTestEvent, {"webhook_id": "wh1"}),
}


def envelope(type_: str, data: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "id": "01a15ca2",
            "type": type_,
            "api_version": "2026-10-19",
            "occurred_at": "2026-10-19T10:10:35.123456789Z",
            "data": data,
        }
    ).encode()


def test_every_documented_type_has_a_model() -> None:
    assert set(EVENT_TYPES) | {"webhook.test"} == set(DATA)


@pytest.mark.parametrize("type_", sorted(DATA))
def test_parse_every_event_type(type_: str) -> None:
    model, data = DATA[type_]
    event = parse_event(envelope(type_, data))
    assert type(event) is model
    assert event.type == type_ and event.occurred_at.year == 2026


def test_typed_fields() -> None:
    ended = parse_event(envelope("call.ended", DATA["call.ended"][1]))
    assert isinstance(ended, CallEndedEvent)
    assert ended.data.usage is not None and ended.data.usage.amount == Decimal("11.0000")
    assert ended.data.session.billable_seconds == 605
    left = parse_event(envelope("participant.left", DATA["participant.left"][1]))
    assert isinstance(left, ParticipantLeftEvent) and left.data.participant.disconnect_reason == "CLIENT_INITIATED"
    chat = parse_event(envelope("chat.message.created", DATA["chat.message.created"][1]))
    assert isinstance(chat, ChatMessageCreatedEvent) and chat.data.message.sender_user_id == "u1"


def test_unknown_type_falls_back() -> None:
    event = parse_event(envelope("call.teleported", {"call": CALL, "where": "mars"}))
    assert isinstance(event, UnknownEvent)
    assert event.type == "call.teleported" and event.data["where"] == "mars"


def test_extra_fields_are_kept() -> None:
    event = parse_event(envelope("webhook.test", {"webhook_id": "wh1", "new_field": 1}))
    assert isinstance(event, WebhookTestEvent) and event.data.model_extra == {"new_field": 1}


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"id": "x"}', envelope("call.ended", {"call": CALL})])
def test_bad_payloads(body: bytes) -> None:
    with pytest.raises(WebhookPayloadError):
        parse_event(body)


def test_construct_event_verifies_then_parses() -> None:
    secret = new_secret()
    body = envelope("webhook.test", {"webhook_id": "wh1"})
    h = headers(sign_webhook(secret, "e1", TS, body), msg_id="e1")
    assert isinstance(construct_event(secret, h, body, now=TS), WebhookTestEvent)
    with pytest.raises(WebhookVerificationError):
        construct_event(new_secret(), h, body, now=TS)
