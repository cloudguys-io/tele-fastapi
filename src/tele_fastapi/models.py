"""Typed request and response models shared by :class:`TeleClient` and :class:`AsyncTeleClient`.

Response models keep unknown fields (``extra="allow"``) so a newer Tele server never breaks an older SDK.
Money is a :class:`~decimal.Decimal` (Tele sends decimal strings with 4 places). Times are timezone-aware.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "APIClient",
    "AccessPolicy",
    "Call",
    "CallMember",
    "CallStatus",
    "CallUser",
    "ChatMessage",
    "Denial",
    "ICEServer",
    "Invite",
    "InviteOptions",
    "InvitePreview",
    "JoinResponse",
    "JoinUser",
    "Me",
    "PostedMessage",
    "PreviewCall",
    "PreviewInvite",
    "Rate",
    "RateScope",
    "Recording",
    "RotatedWebhookSecret",
    "Session",
    "SessionParticipant",
    "UsageReport",
    "UsageRow",
    "User",
    "WebhookDelivery",
    "WebhookDeliveryRetry",
    "WebhookEndpoint",
    "WebhookTestResult",
]

CallStatus = Literal["scheduled", "live", "ended", "cancelled"]
AccessPolicy = Literal["listed_only", "any_client_user"]


class TeleModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- inputs ----


class CallUser(_Input):
    """A user in a request body: a call's ``host`` or one of its ``members``."""

    user_id: str
    name: str | None = None
    team: str | None = None


class JoinUser(_Input):
    """The end user a service token joins on behalf of. ``ip`` (the end user's IP) is required."""

    user_id: str
    ip: str
    name: str | None = None
    team: str | None = None
    user_agent: str | None = None


class InviteOptions(_Input):
    label: str | None = None
    expires_at: datetime | None = None
    max_uses: int | None = None


# ---- outputs ----


class User(TeleModel):
    user_id: str
    name: str
    team: str | None = None


class Me(TeleModel):
    """``GET /v1/me``: who the token represents."""

    client: str
    scope: Literal["user", "service"]
    user: User | None = None


class CallMember(TeleModel):
    user_id: str
    name: str
    role: Literal["host", "participant"]
    added_at: datetime


class Invite(TeleModel):
    id: str
    code: str | None = None
    """Only returned when the invite is created."""
    url: str | None = None
    """Only at creation, and only if your key has an invite URL template."""
    label: str = ""
    expires_at: datetime | None = None
    max_uses: int | None = None
    use_count: int = 0
    revoked_at: datetime | None = None
    created_at: datetime


class Call(TeleModel):
    id: str
    title: str
    status: CallStatus
    access_policy: AccessPolicy
    host_user_id: str
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    join_opens_at: datetime | None = None
    join_closes_at: datetime | None = None
    join_opens_before_minutes: int
    join_closes_after_minutes: int
    max_participants: int
    max_duration_minutes: int
    record: bool
    created_at: datetime
    cancelled_at: datetime | None = None
    ended_at: datetime | None = None
    members: list[CallMember] = Field(default_factory=list)
    """Present on create and get; empty in lists and after edits."""
    invite: Invite | None = None
    """The default invite, with its one-time ``code``; only on create."""


class Denial(TeleModel):
    """Why a user can't join now (``reason`` of an invite preview)."""

    code: str
    message: str
    opens_at: datetime | None = None


class PreviewCall(TeleModel):
    id: str
    title: str
    status: CallStatus
    host: User
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    join_opens_at: datetime | None = None
    join_closes_at: datetime | None = None


class PreviewInvite(TeleModel):
    label: str = ""
    expires_at: datetime | None = None


class InvitePreview(TeleModel):
    """``GET /v1/invites/{code}``: what to show on a lobby screen."""

    call: PreviewCall
    invite: PreviewInvite
    can_join_now: bool
    reason: Denial | None = None


class ICEServer(TeleModel):
    urls: list[str]
    username: str | None = None
    credential: str | None = None


class JoinResponse(TeleModel):
    """Everything the media SDK needs to connect."""

    ws_url: str
    token: str
    token_expires_at: datetime
    ice_servers: list[ICEServer] | None = None
    """``None`` means: don't pass any ICE servers, use the SDK defaults."""
    identity: str
    grant_id: str
    role: Literal["host", "participant"]
    call: Call


class ChatMessage(TeleModel):
    id: str
    stream_id: str
    user_id: str
    name: str
    body: str
    sent_at: datetime


class PostedMessage(TeleModel):
    """Result of posting a chat message: ``created`` is False when it was already recorded."""

    stream_id: str
    created: bool


class Recording(TeleModel):
    id: str
    call_id: str
    state: str
    """``recording``, ``processing``, ``available``, ``failed``, ``archived``, ``restoring``, ``restored``,
    or ``expired``."""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    size_bytes: int | None = None
    error: str | None = None
    restored_until: datetime | None = None


class SessionParticipant(TeleModel):
    id: str
    user_id: str
    name: str
    joined_at: datetime
    left_at: datetime | None = None
    duration_seconds: int
    ip: str | None = None
    """``None`` after the 30-day retention window."""
    user_agent: str | None = None
    platform: str = ""
    disconnect_reason: str | None = None
    team: str | None = None
    identity: str = ""
    participant_sid: str = ""
    grant_id: str | None = None
    ip_source: str | None = None
    last_seen_at: datetime | None = None
    user_internal_id: str | None = None


class Session(TeleModel):
    """One room session of a call, with its participants and charges."""

    id: str
    status: str
    """``open`` while the call runs, then ``closed``, then ``billed``."""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int
    billable_seconds: int | None = None
    billable_minutes: Decimal | None = None
    amount: Decimal | None = None
    currency: str | None = None
    peak_participants: int
    unique_participants: int
    end_reason: str | None = None
    participants: list[SessionParticipant] = Field(default_factory=list)
    room_sid: str = ""
    room_name: str = ""
    closed_at: datetime | None = None
    closed_by: str | None = None


class WebhookEndpoint(TeleModel):
    id: str
    url: str
    description: str = ""
    events: list[str]
    enabled: bool
    disabled_at: datetime | None = None
    disabled_reason: str | None = None
    failing_since: datetime | None = None
    created_at: datetime
    updated_at: datetime
    secret: str | None = None
    """Only returned at creation: store it now."""


class RotatedWebhookSecret(TeleModel):
    secret: str
    previous_secret_valid_until: datetime


class WebhookTestResult(TeleModel):
    event_id: str


class WebhookDelivery(TeleModel):
    id: str
    event_id: str
    type: str
    status: str
    """``pending``, ``delivered`` or ``failed``."""
    attempts: int
    next_attempt_at: datetime | None = None
    last_status: int | None = None
    last_error: str | None = None
    last_attempt_at: datetime | None = None
    created_at: datetime
    delivered_at: datetime | None = None


class WebhookDeliveryRetry(TeleModel):
    id: str
    status: str


class APIClient(TeleModel):
    """An API key made for one of your tenants (admin keys only)."""

    id: str
    name: str
    api_key: str
    owner_ref: str | None = None
    invite_url_template: str | None = None
    created_at: datetime
    revoked_at: datetime | None = None
    api_secret: str | None = None
    """Only at creation and rotation."""


class UsageRow(TeleModel):
    group: dict[str, str]
    sessions: int
    room_seconds: int
    room_minutes: Decimal
    recording_minutes: Decimal
    adjustments: Decimal
    amount: Decimal
    currency: str


class UsageReport(TeleModel):
    from_: datetime = Field(alias="from")
    to: datetime
    group_by: str
    rows: list[UsageRow]


class RateScope(TeleModel):
    api_client_id: str | None = None
    team_id: str | None = None


class Rate(TeleModel):
    id: str
    name: str
    scope: RateScope
    room_minute_price: Decimal
    recording_minute_price: Decimal
    currency: str
    rounding: str
    """``ceil_minute`` or ``per_second``."""
    min_billable_minutes: int
    min_participants: int
    effective_from: datetime
    effective_to: datetime | None = None
