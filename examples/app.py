"""A small consumer backend built on tele-fastapi.

    export TELE_API_KEY=... TELE_API_SECRET=...      # your Tele key (never ship the secret to apps)
    export TELE_API_URL=http://127.0.0.1:8080        # optional, defaults to https://tele-api.cloudguys.io
    export TELE_WEBHOOK_SECRET=whsec_...             # optional, enables POST /api/tele/webhooks
    uv run uvicorn app:app --app-dir examples --reload

Authentication is faked with an ``X-Demo-User: <user id>`` header so the example runs on its own. Replace
``current_user`` with your real session check.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from tele_fastapi import (
    DEFAULT_BASE_URL,
    AsyncTeleClient,
    CallUser,
    TeleError,
    TeleSigner,
    TeleUser,
)
from tele_fastapi.fastapi import (
    TeleClientDep,
    TeleErrorRoute,
    TeleWebhook,
    tele_error_response,
    tele_join_router,
    tele_lifespan,
    tele_token_router,
)
from tele_fastapi.webhooks import CallEndedEvent, ChatMessageCreatedEvent, TeleEvent

log = logging.getLogger("tele-example")


def current_user(x_demo_user: str = Header("")) -> TeleUser:
    """Your authentication goes here. The demo trusts a header, which a real app must never do."""
    if not x_demo_user:
        raise HTTPException(401, "sign in first (send X-Demo-User)")
    return TeleUser(id=x_demo_user, name=x_demo_user.title(), team="demo")


class NewCall(BaseModel):
    title: str
    members: list[str] = []
    record: bool = False


def create_app(signer: TeleSigner, *, base_url: str = DEFAULT_BASE_URL, webhook_secret: str | None = None) -> FastAPI:
    app = FastAPI(
        title="tele-fastapi example", lifespan=tele_lifespan(lambda: AsyncTeleClient(signer, base_url=base_url))
    )

    # The shared routes the Tele UI and client SDKs call (prefix /api/tele):
    #   POST /api/tele/token, GET /api/tele/invites/{code}, POST /api/tele/invites/{code}/join,
    #   POST /api/tele/calls/{id}/join, GET /api/tele/calls/{id}/messages, POST /api/tele/calls/{id}/diagnostics
    app.include_router(tele_token_router(signer, current_user))
    app.include_router(tele_join_router(current_user))

    # The app's own routes. TeleErrorRoute answers errors in Tele's {"error": {"code", "message"}} shape.
    api = APIRouter(prefix="/api", route_class=TeleErrorRoute)

    @api.post("/calls", status_code=201)
    async def create_call(new: NewCall, tele: TeleClientDep, user: TeleUser = Depends(current_user)) -> Any:
        """Your backend creates the call with its service token; the signed-in user hosts it."""
        try:
            call = await tele.create_call(
                new.title,
                host=CallUser(user_id=user.id, name=user.name, team=user.team),
                members=[CallUser(user_id=m) for m in new.members],
                record=new.record,
            )
        except TeleError as e:
            return tele_error_response(e)
        assert call.invite is not None
        return {"id": call.id, "status": call.status, "invite_code": call.invite.code, "invite_url": call.invite.url}

    @api.get("/calls")
    async def my_calls(tele: TeleClientDep, user: TeleUser = Depends(current_user)) -> Any:
        """Calls the user is a member of, read with their own user token."""
        try:
            calls = await tele.as_user(user).list_calls(status=["scheduled", "live"])
        except TeleError as e:
            return tele_error_response(e)
        return [{"id": c.id, "title": c.title, "status": c.status} for c in calls]

    @api.post("/calls/{call_id}/end")
    async def end_for_all(call_id: str, tele: TeleClientDep, user: TeleUser = Depends(current_user)) -> Any:
        """``endForAll()`` in the call UI: only the host's token may end the call."""
        try:
            return (await tele.as_user(user).end_call(call_id)).model_dump(mode="json")
        except TeleError as e:
            return tele_error_response(e)

    @api.get("/recordings/{recording_id}/download")
    async def download(
        recording_id: str, request: Request, tele: TeleClientDep, user: TeleUser = Depends(current_user)
    ) -> Response:
        """Proxy the MP4 so a ``<video>`` tag can play and seek it (Range is passed through)."""
        try:
            video = await tele.as_user(user).open_recording(recording_id, range=request.headers.get("range"))
        except TeleError as e:
            return tele_error_response(e)
        return StreamingResponse(
            video.aiter_bytes(),
            status_code=video.status_code,
            headers=video.passthrough_headers(),
            background=BackgroundTask(video.aclose),
        )

    if webhook_secret:

        @api.post("/tele/webhooks", status_code=204, response_model=None)
        async def tele_webhook(event: TeleEvent = Depends(TeleWebhook(webhook_secret))) -> None:
            if isinstance(event, CallEndedEvent) and event.data.usage:
                log.info("call %s cost %s %s", event.data.call.id, event.data.usage.amount, event.data.usage.currency)
            elif isinstance(event, ChatMessageCreatedEvent):
                log.info("new chat message %s in %s", event.data.message.id, event.data.call_id)
            else:
                log.info("tele event %s", event.type)

    app.include_router(api)
    return app


def create_app_from_env() -> FastAPI:
    signer = TeleSigner(os.environ["TELE_API_KEY"], os.environ["TELE_API_SECRET"])
    return create_app(
        signer,
        base_url=os.environ.get("TELE_API_URL", DEFAULT_BASE_URL),
        webhook_secret=os.environ.get("TELE_WEBHOOK_SECRET"),
    )


app = create_app_from_env()
