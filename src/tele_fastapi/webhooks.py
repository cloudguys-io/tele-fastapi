"""Verify and parse Tele webhooks.

Tele signs every delivery with `Standard Webhooks <https://www.standardwebhooks.com/>`_: the endpoint secret is
``whsec_`` + base64 of random bytes and ``webhook-signature`` holds one or more space-separated
``v1,<base64 HMAC-SHA256(secret, id + "." + timestamp + "." + body)>`` values. During a secret rotation Tele
sends two signatures, one per secret, so either verifies.

Always verify the **raw** request body, before parsing it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, ValidationError

__all__ = [
    "EVENT_TYPES",
    "WEBHOOK_TOLERANCE_SECONDS",
    "CallCancelledEvent",
    "CallCreatedEvent",
    "CallEndedEvent",
    "CallStartedEvent",
    "CallUpdatedEvent",
    "ChatMessageCreatedEvent",
    "ParticipantJoinedEvent",
    "ParticipantLeftEvent",
    "RecordingFailedEvent",
    "RecordingReadyEvent",
    "RecordingStartedEvent",
    "TeleEvent",
    "UnknownEvent",
    "WebhookPayloadError",
    "WebhookTestEvent",
    "WebhookVerificationError",
    "construct_event",
    "parse_event",
    "sign_webhook",
    "verify_webhook",
]

WEBHOOK_TOLERANCE_SECONDS = 300
_SECRET_PREFIX = "whsec_"


class WebhookVerificationError(Exception):
    """The request is not a genuine Tele webhook (missing headers, stale timestamp, or a bad signature)."""


class WebhookPayloadError(ValueError):
    """The signature was valid but the body is not a Tele event envelope."""


Headers = Mapping[str, str]


def _header(headers: Headers, name: str) -> str:
    value = headers.get(name)
    if value is None:  # plain dicts are case-sensitive; Starlette and httpx headers are not
        for k, v in headers.items():
            if k.lower() == name:
                return v
        return ""
    return value


def _key(secret: str) -> bytes:
    try:
        return base64.b64decode(secret.removeprefix(_SECRET_PREFIX), validate=True)
    except (binascii.Error, ValueError):
        raise WebhookVerificationError("webhook secret is not valid base64") from None


def sign_webhook(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    """Return the ``v1,<base64>`` signature Tele would send for this message. Useful in tests."""
    mac = hmac.new(_key(secret), f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256)
    return "v1," + base64.b64encode(mac.digest()).decode()


def verify_webhook(
    secret: str | Iterable[str],
    headers: Headers,
    body: bytes | str,
    *,
    now: float | None = None,
    tolerance: int = WEBHOOK_TOLERANCE_SECONDS,
) -> str:
    """Check a Tele webhook and return its ``webhook-id``.

    ``secret`` is the endpoint secret (``whsec_...``), or several secrets if you are switching between them
    yourself. ``headers`` is any mapping (Starlette, httpx or a dict). ``body`` is the raw body exactly as
    received. Raises :class:`WebhookVerificationError` if the message is not from Tele or is more than
    ``tolerance`` seconds (default 5 minutes) away from ``now``.
    """
    msg_id = _header(headers, "webhook-id")
    ts = _header(headers, "webhook-timestamp")
    sigs = _header(headers, "webhook-signature")
    if not msg_id or not ts or not sigs:
        raise WebhookVerificationError("missing webhook-id, webhook-timestamp or webhook-signature")
    try:
        sent = int(ts)
    except ValueError:
        raise WebhookVerificationError("webhook timestamp is too old or in the future") from None
    current = time.time() if now is None else now
    if abs(current - sent) > tolerance:
        raise WebhookVerificationError("webhook timestamp is too old or in the future")

    raw = body.encode() if isinstance(body, str) else body
    secrets = [secret] if isinstance(secret, str) else list(secret)
    if not secrets:
        raise WebhookVerificationError("no webhook secret configured")
    received = [s.encode() for s in sigs.split()]
    for sec in secrets:
        want = sign_webhook(sec, msg_id, sent, raw).encode()
        if any(hmac.compare_digest(got, want) for got in received):
            return msg_id
    raise WebhookVerificationError("webhook signature does not match")


# ---- event models ----


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class CallRef(_Model):
    id: str
    title: str
    host_user_id: str


class WebhookCall(_Model):
    id: str
    title: str
    status: str
    host_user_id: str
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    record: bool = False
    created_at: datetime
    cancelled_at: datetime | None = None
    ended_at: datetime | None = None


class CallChangedData(_Model):
    call: WebhookCall


class CallStartedData(_Model):
    call: CallRef
    session_id: str
    started_at: datetime


class EndedSession(_Model):
    id: str
    started_at: datetime | None = None
    ended_at: datetime
    end_reason: str | None = None
    billable_seconds: int | None = None
    peak_participants: int
    unique_participants: int


class SessionUsage(_Model):
    metric: str
    billable_minutes: Decimal
    amount: Decimal
    currency: str


class CallEndedData(_Model):
    call: CallRef
    session: EndedSession
    usage: SessionUsage | None = None


class WebhookParticipant(_Model):
    session_id: str
    participant_sid: str
    user_id: str
    name: str
    platform: str = ""
    joined_at: datetime
    left_at: datetime | None = None
    disconnect_reason: str | None = None


class ParticipantData(_Model):
    call: CallRef
    participant: WebhookParticipant


class WebhookRecording(_Model):
    id: str
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    size_bytes: int | None = None
    error: str | None = None


class RecordingData(_Model):
    call: CallRef
    recording: WebhookRecording


class WebhookChatMessage(_Model):
    id: str
    stream_id: str
    sender_user_id: str
    sent_at: datetime


class ChatMessageData(_Model):
    call_id: str
    message: WebhookChatMessage


class WebhookTestData(_Model):
    webhook_id: str


class _Event(_Model):
    id: str
    type: str
    api_version: str
    occurred_at: datetime


class CallCreatedEvent(_Event):
    type: Literal["call.created"]
    data: CallChangedData


class CallUpdatedEvent(_Event):
    """A call was changed, or ended by the host or the scheduler."""

    type: Literal["call.updated"]
    data: CallChangedData


class CallCancelledEvent(_Event):
    type: Literal["call.cancelled"]
    data: CallChangedData


class CallStartedEvent(_Event):
    """The first person joined a room session."""

    type: Literal["call.started"]
    data: CallStartedData


class CallEndedEvent(_Event):
    """A room session closed; carries duration and usage."""

    type: Literal["call.ended"]
    data: CallEndedData


class ParticipantJoinedEvent(_Event):
    type: Literal["participant.joined"]
    data: ParticipantData


class ParticipantLeftEvent(_Event):
    type: Literal["participant.left"]
    data: ParticipantData


class RecordingStartedEvent(_Event):
    type: Literal["recording.started"]
    data: RecordingData


class RecordingReadyEvent(_Event):
    type: Literal["recording.ready"]
    data: RecordingData


class RecordingFailedEvent(_Event):
    type: Literal["recording.failed"]
    data: RecordingData


class ChatMessageCreatedEvent(_Event):
    """A chat message was saved. Ids only: read the text from the chat history API."""

    type: Literal["chat.message.created"]
    data: ChatMessageData


class WebhookTestEvent(_Event):
    """Sent by ``POST /v1/webhooks/{id}/test``."""

    type: Literal["webhook.test"]
    data: WebhookTestData


class UnknownEvent(_Event):
    """An event type this SDK version does not know yet (for example one added after release)."""

    data: dict[str, Any] = {}


TeleEvent = Union[  # noqa: UP007 - a runtime alias must work on Python 3.10
    CallCreatedEvent,
    CallUpdatedEvent,
    CallCancelledEvent,
    CallStartedEvent,
    CallEndedEvent,
    ParticipantJoinedEvent,
    ParticipantLeftEvent,
    RecordingStartedEvent,
    RecordingReadyEvent,
    RecordingFailedEvent,
    ChatMessageCreatedEvent,
    WebhookTestEvent,
    UnknownEvent,
]

_MODELS: dict[str, type[_Event]] = {
    "call.created": CallCreatedEvent,
    "call.updated": CallUpdatedEvent,
    "call.cancelled": CallCancelledEvent,
    "call.started": CallStartedEvent,
    "call.ended": CallEndedEvent,
    "participant.joined": ParticipantJoinedEvent,
    "participant.left": ParticipantLeftEvent,
    "recording.started": RecordingStartedEvent,
    "recording.ready": RecordingReadyEvent,
    "recording.failed": RecordingFailedEvent,
    "chat.message.created": ChatMessageCreatedEvent,
    "webhook.test": WebhookTestEvent,
}

EVENT_TYPES: tuple[str, ...] = tuple(t for t in _MODELS if t != "webhook.test")
"""Event types an endpoint can subscribe to (``["*"]`` subscribes to all of them)."""


def parse_event(body: bytes | str | Mapping[str, Any]) -> TeleEvent:
    """Parse an already verified body into a typed event; unknown types become :class:`UnknownEvent`."""
    try:
        raw: Any = json.loads(body) if isinstance(body, (bytes, str)) else dict(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise WebhookPayloadError(f"webhook body is not JSON: {e}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
        raise WebhookPayloadError("webhook body is not a Tele event envelope")
    model = _MODELS.get(raw["type"], UnknownEvent)
    try:
        event = model.model_validate(raw)
    except ValidationError as e:
        raise WebhookPayloadError(f"invalid {raw['type']} event: {e}") from None
    return event  # type: ignore[return-value]


def construct_event(
    secret: str | Iterable[str],
    headers: Headers,
    body: bytes | str,
    *,
    now: float | None = None,
    tolerance: int = WEBHOOK_TOLERANCE_SECONDS,
) -> TeleEvent:
    """:func:`verify_webhook`, then :func:`parse_event`."""
    verify_webhook(secret, headers, body, now=now, tolerance=tolerance)
    return parse_event(body)
