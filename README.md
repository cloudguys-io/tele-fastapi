# tele-fastapi

The Python server SDK for [Tele](https://tele-api.cloudguys.io), the hosted video-calling API, with first-class
FastAPI integration.

- `TeleSigner` signs user and service tokens (HS256, the same claims and limits as the Go and Node helpers).
- `TeleClient` (sync) and `AsyncTeleClient` (async) cover every endpoint in the Tele guide with typed pydantic models.
  Built from a signer, they mint and cache service tokens for you; `.as_user(user)` acts as one of your users.
- `tele_fastapi.webhooks` verifies Tele's Standard Webhooks signatures and parses every event into a typed model.
- `tele_fastapi.fastapi` gives you a token endpoint, a join proxy for the call UI and mobile apps, a webhook
  dependency and a shared client for your app's lifespan.

The core needs only `httpx`, `pydantic` (v2) and `PyJWT`. FastAPI is an optional extra. Python 3.10 or newer.

## Install

From the `v0.1.0` tag:

```bash
pip install "tele-fastapi[fastapi] @ git+https://github.com/cloudguys-io/tele-fastapi@v0.1.0"
# or
uv add "tele-fastapi[fastapi] @ git+https://github.com/cloudguys-io/tele-fastapi@v0.1.0"
```

Leave out `[fastapi]` if you only need the client, tokens and webhooks.

## Quickstart

```python
import os
from fastapi import Depends, FastAPI, HTTPException, Request
from tele_fastapi import AsyncTeleClient, CallUser, TeleSigner, TeleUser
from tele_fastapi.fastapi import TeleClientDep, tele_join_router, tele_lifespan, tele_token_router

signer = TeleSigner(os.environ["TELE_API_KEY"], os.environ["TELE_API_SECRET"])
app = FastAPI(lifespan=tele_lifespan(lambda: AsyncTeleClient(signer)))


def current_user(request: Request) -> TeleUser:
    """Your own authentication: look up the signed-in user, or answer 401."""
    session = request.session.get("user")  # for example with Starlette's SessionMiddleware
    if not session:
        raise HTTPException(401, "sign in first")
    return TeleUser(id=session["id"], name=session["name"], team=session["org"])


app.include_router(tele_token_router(signer, current_user))  # POST /api/tele/token
app.include_router(tele_join_router(current_user))  # /api/tele/invites/{code}[/join], /api/tele/calls/{id}/...


@app.post("/calls")
async def create_call(tele: TeleClientDep, user: TeleUser = Depends(current_user)):
    call = await tele.create_call("Design review", host=CallUser(user_id=user.id, name=user.name))
    return {"id": call.id, "invite_code": call.invite.code}
```

In the browser, the prebuilt call UI joins through your backend:

```js
call.getJoin = () => fetch(`/api/tele/invites/${code}/join`, { method: "POST" })
  .then((r) => r.ok ? r.json() : r.json().then((b) => Promise.reject(b.error)));
```

[`examples/app.py`](examples/app.py) is a complete, runnable app using everything below.

## Tokens

```python
from datetime import timedelta
from tele_fastapi import TeleSigner, TeleUser

signer = TeleSigner(api_key, api_secret)
user_token = signer.user_token(TeleUser(id="user-123", name="Rahim", team="sales"), ttl=900)  # ≤ 1 hour
service_token = signer.service_token(ttl=timedelta(minutes=5))  # ≤ 10 minutes
```

Claims: `iss` (your key), `sub` (user tokens), `iat`, `nbf`, `exp`, optional `name` and `team`, and
`tele.scope` (`"user"` or `"service"`). `id` is required and at most 128 characters; `name` and `team` at most 64.
Invalid input raises `TokenError` (a `ValueError`).

**`TeleUser` or `CallUser`?** `TeleUser(id, name, team)` is *who a token acts as*: pass it to `user_token`,
`as_user` and the FastAPI routers. `CallUser(user_id, name, team)` is *a user inside a request body*: a call's
`host` or `members` (and `JoinUser` adds `ip` and `user_agent` for service-token joins).

## Clients

```python
from tele_fastapi import TeleClient, AsyncTeleClient, TeleUser

tele = TeleClient(signer)  # service tokens, minted and cached automatically
tele = TeleClient(signer, base_url="http://127.0.0.1:8080")
alice = tele.as_user(TeleUser(id="alice", name="Alice"))  # user tokens for alice, same connection pool
static = TeleClient(token=some_bearer_token)  # a token you already have
tenant = admin.for_tenant(tenant_client_id)  # admin keys: sends Tele-Client for webhook routes
```

`AsyncTeleClient` has the same constructor and methods, awaited, and closes with `await tele.aclose()` or
`async with`. Pass `http_client=` to use your own `httpx.Client` / `httpx.AsyncClient` (it is then yours to close),
`timeout=`, `headers=`, or `service_token_ttl=`. The default base URL is `https://tele-api.cloudguys.io`.

Optional arguments left as `None` are not sent, so Tele applies its defaults or leaves the field unchanged.
Datetimes must be timezone-aware. Money fields are `Decimal`. Enumerated response fields (`status`, `role`,
`access_policy`, `scope`, recording `state`...) are plain strings, so a value added to Tele later never breaks
parsing. Secrets and tokens (`api_secret`, webhook `secret`, join `token`, TURN credentials, invite `code`/`url`)
are left out of `repr()` and `str()`.

| Area | Methods |
|---|---|
| Calls | `create_call`, `list_calls`, `get_call`, `update_call`, `cancel_call`, `end_call` |
| Members, sessions | `put_member`, `remove_member`, `list_sessions` |
| Invites | `create_invite`, `list_invites`, `revoke_invite`, `preview_invite` |
| Joining | `join_invite(code, platform=, forwarded_for=, user_agent=)`, `join_call(call_id, user=JoinUser(...), platform=...)` |
| Chat | `list_messages(call_id, after=, limit=)`, `post_message(call_id, stream_id, body, sent_at=)` |
| Recordings | `start_recording`, `stop_recording`, `list_recordings`, `get_recording`, `restore_recording`, `open_recording`, `stream_recording`, `download_recording` |
| Diagnostics | `report_diagnostic(call_id, body)` (forward the `<tele-call>` diagnostic body) |
| Webhooks | `create_webhook`, `list_webhooks`, `get_webhook`, `update_webhook`, `delete_webhook`, `rotate_webhook_secret`, `test_webhook`, `list_webhook_deliveries`, `retry_webhook_delivery` |
| Tenant keys (admin) | `create_client`, `list_clients`, `revoke_client`, `rotate_client_secret` |
| Usage, rates | `usage(from_=, to=, group_by=, team=, api_key=)`, `usage_csv(...)`, `list_rates`, `set_rate`, `end_rate` (admin) |
| Other | `me`, `health`, `ready`, `raw_request` (any request, no raising) |

### Creating and joining

```python
from tele_fastapi import CallUser, JoinUser

call = tele.create_call(
    "Support session #4411",
    host=CallUser(user_id="agent-7", name="Agent Sumi", team="support"),  # required with a service token
    members=[CallUser(user_id="cust-9001", name="Customer")],
    max_participants=2,
    invite={"label": "email link", "max_uses": 1},
)
code = call.invite.code  # one-time: store or send it now

# as the user (their app called your backend)
join = tele.as_user(TeleUser(id="cust-9001")).join_invite(code, platform="android")

# or on the user's behalf with the service token: Tele needs the end user's IP
join = tele.join_call(call.id, user=JoinUser(user_id="cust-9001", ip="203.0.113.50", user_agent="YourApp/3.2"))

join.ws_url, join.token, join.ice_servers  # hand these to the media SDK as is (ice_servers may be None)
```

### Errors

Every non-2xx response raises a `TeleError` with `status`, `code`, `message`, `opens_at` (for `too_early`),
`request_id` (from an `X-Request-Id` response header, when a proxy adds one) and the parsed `body`. Subclasses by
status: `BadRequestError` (400), `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (404),
`ConflictError` (409), `GoneError` (410), `RateLimitedError` (429), `NotImplementedByServerError` (501) and
`ServerError` (5xx). A 2xx body that doesn't fit the model raises `TeleResponseError` (`code == "invalid_response"`,
raw body in `body`). Helpers: `err.retryable`, `err.is_join_window`, `err.is_invite_invalid`, `err.is_call_closed`.
Branch on `code`:

```python
from tele_fastapi import ConflictError

try:
    tele.as_user(user).join_invite(code)
except ConflictError as e:
    if e.code == "too_early":
        show_countdown(e.opens_at)
    else:
        raise
```

Network failures surface as `httpx` exceptions. Nothing is retried automatically: `POST /v1/calls` is not
idempotent and each successful join uses up one use of a `max_uses` invite.

### Recordings

`open_recording` streams the MP4 and passes `Range` through, so you can proxy it to a `<video>` tag:

```python
from starlette.background import BackgroundTask
from fastapi.responses import StreamingResponse


@app.get("/recordings/{rec_id}")
async def recording(rec_id: str, request: Request, tele: TeleClientDep):
    video = await tele.open_recording(rec_id, range=request.headers.get("range"))  # raises TeleError first
    return StreamingResponse(
        video.aiter_bytes(),
        status_code=video.status_code,
        headers=video.passthrough_headers(),
        background=BackgroundTask(video.aclose),
    )
```

`with tele.stream_recording(rec_id) as video:` (sync) or `async with` (async) closes it for you, and
`download_recording(rec_id)` reads it into memory. Archived recordings raise `ConflictError` (`archived`):
call `restore_recording` and retry in a few hours.

## Webhooks

```python
from tele_fastapi.webhooks import CallEndedEvent, construct_event, verify_webhook

event = construct_event(secret, request.headers, raw_body)  # verify, then parse
if isinstance(event, CallEndedEvent) and event.data.usage:
    record_charge(event.data.call.id, event.data.usage.amount)
```

`verify_webhook(secret, headers, body)` checks `webhook-id`, `webhook-timestamp` (5 minutes tolerance) and every
signature in `webhook-signature`, and returns the `webhook-id` (use it to drop duplicates). During a secret
rotation Tele sends two signatures, so the old and new secrets both verify; you can also pass a list of secrets.
It raises `WebhookVerificationError`. `parse_event(body)` returns one of `CallCreatedEvent`, `CallUpdatedEvent`,
`CallCancelledEvent`, `CallStartedEvent`, `CallEndedEvent`, `ParticipantJoinedEvent`, `ParticipantLeftEvent`,
`RecordingStartedEvent`, `RecordingReadyEvent`, `RecordingFailedEvent`, `ChatMessageCreatedEvent`,
`WebhookTestEvent`, or `UnknownEvent` for types this version doesn't know (raising `WebhookPayloadError` on a body
that isn't an event). Always verify the raw body before parsing it.

## FastAPI

### Shared client

```python
from tele_fastapi.fastapi import TeleClientDep, get_tele, tele_lifespan

app = FastAPI(lifespan=tele_lifespan(lambda: AsyncTeleClient(signer)))


@app.get("/usage")
async def usage(tele: TeleClientDep):  # or: tele: AsyncTeleClient = Depends(get_tele)
    return await tele.usage(group_by="team")
```

The client is created at startup and closed at shutdown. To combine it with your own lifespan, enter it inside
yours: `async with tele_lifespan(factory)(app): yield`.

### Shared backend routes

The routers serve the route convention every Tele SDK shares (prefix `/api/tele`, configurable):

| Method | Path | Does | Response |
|---|---|---|---|
| POST | `/api/tele/token` | user token for the signed-in user | `{"token": "...", "expires_at": "RFC3339"}` |
| GET | `/api/tele/invites/{code}` | invite preview, as the user (user token) | Tele's body unchanged |
| POST | `/api/tele/invites/{code}/join` | invite join, as the user (user token) | Tele's join response unchanged |
| POST | `/api/tele/calls/{id}/join` | call join, service token with `user: {user_id, ip, user_agent, name?, team?}` | Tele's join response unchanged |
| GET | `/api/tele/calls/{id}/messages` | chat history as the user (`after`, `limit` passed through) | Tele's body unchanged |
| POST | `/api/tele/calls/{id}/diagnostics` | forwards the call UI's diagnostic body as the user | Tele's status and body |

`tele_token_router(signer, get_user, path="/api/tele/token", ttl=900)` serves the first route; `expires_at` is
the token's own `exp`. `tele_join_router(get_user, prefix="/api/tele")` serves the rest (`diagnostics=False`
leaves out the last one). `get_user` is your own authentication: a dependency (or `Depends(...)`) returning a
`TeleUser`, or raising `HTTPException(401)` for anonymous requests.

`X-Client-Platform` from the app is forwarded (fallback `default_platform="web"`; tele-flutter sends
`flutter-<os>`), and so is the user agent.

**Errors** always use Tele's shape `{"error": {"code": "...", "message": "..."}}`, including the backend's own:

| Situation | Answer |
|---|---|
| not signed in (`get_user` raises 401) | `401 unauthenticated` |
| Tele 4xx other than 401 | relayed unchanged |
| Tele 401 (your backend's credentials) | `502 upstream_auth` |
| Tele 5xx | `502 upstream_error` |
| Tele timed out | `503 upstream_timeout` |
| Tele unreachable | `502 upstream_unreachable` |
| bad path segment (`.`, `..`) | `404 not_found` / `invite_not_found` |
| diagnostic body over 64 KB (refused while streaming) | `413 invalid_body` |

The routers get this from `TeleErrorRoute`, a route class you can reuse for your own routes
(`APIRouter(route_class=TeleErrorRoute)`), and `tele_error_response(err)` turns a `TeleError` from the client into
the same response.

**Client IPs.** Tele records the request's peer IP for user-token calls and trusts no customer proxy on hosted
Tele. So:

- **Call-id joins** are service-token joins carrying the end user's IP, so Tele records the correct IP
  (`service_join=True`, the default; `service_join=False` forwards the user's token instead and is only for a Tele
  that trusts your backend as a proxy).
- **Invite joins through a backend record the backend's IP.** Browsers can't avoid this (Tele sends no CORS
  headers). Native apps can, by calling `POST /v1/invites/{code}/join` directly with a user token from
  `/api/tele/token`.
- After the first invite join, rejoin through the call-id route (`POST /api/tele/calls/{id}/join`).

The end user's IP comes from `request.client.host`; behind a reverse proxy run uvicorn with
`--proxy-headers --forwarded-allow-ips=...`, or pass `client_ip=lambda request: ...`. Pass `client=` to use a
specific `AsyncTeleClient` instead of the lifespan one.

### Using with tele-nextjs-ui / tele-flutter

The defaults line up, so nothing needs configuring on either side:

```python
app.include_router(tele_token_router(signer, current_user))  # POST /api/tele/token
app.include_router(tele_join_router(current_user))  # the other /api/tele routes
```

- **tele-nextjs-ui** calls `/api/tele/invites/{code}` for the lobby, `/api/tele/invites/{code}/join` and
  `/api/tele/calls/{id}/join` for `getJoin()`, `/api/tele/calls/{id}/messages` for chat history and
  `/api/tele/calls/{id}/diagnostics` for diagnostics, on the same origin.
- **tele-flutter** points at your backend's base URL and calls the same routes with `X-Client-Platform:
  flutter-<os>`; it can also take a user token from `/api/tele/token` to join an invite directly
  (`POST /v1/invites/{code}/join`), so Tele records the device's own IP.

If you change `prefix` or `path` here, set the same base path in the UI or app.

### Webhook dependency

```python
from fastapi import Depends
from tele_fastapi.fastapi import TeleWebhook
from tele_fastapi.webhooks import TeleEvent


@app.post("/api/tele/webhooks", status_code=204, response_model=None)
async def tele_webhook(event: TeleEvent = Depends(TeleWebhook(os.environ["TELE_WEBHOOK_SECRET"]))): ...
```

`TeleWebhook` reads the raw body, verifies it, and yields the typed event. A bad or stale signature answers
`401`, a body that isn't a Tele event `400`. The secret may be a string, a list (while you switch secrets), or a
callable returning either (for secrets you load or rotate at runtime; it answers `503` while that returns nothing).
Malformed secrets in a list are skipped; if none is usable it answers `500` (a configuration error), and
`verify_webhook` raises `WebhookSecretError`.
Answer within 10 seconds and do slow work afterwards; deliveries are at least once and unordered.

## Security notes

- **Your API secret never leaves your backend.** It signs tokens: anyone holding it can act as any of your users.
  Apps get short-lived user tokens from your token endpoint, or never see a Tele token at all with the join proxy.
- Service tokens are for your backend only. Never return one to a browser or app.
- Put your own authentication in front of every route here: the routers trust whatever `get_user` returns.
- Verify webhooks against the raw body before acting on them, and drop duplicate `webhook-id`s.
- `TeleSigner` and the clients never log or print secrets; `repr(signer)` shows only the key.
- Invite codes: treat them like bearer links. Anyone with the code can preview and (per access policy) join;
  don't log them.

## Development

```bash
uv sync
uv run pytest                      # unit tests; integration tests skip without a live API
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python scripts/gen_async.py # after editing endpoint methods in client.py
uv run --isolated --resolution lowest-direct --python 3.10 --group dev pytest tests/unit   # the declared floors
```

The endpoint methods of `AsyncTeleClient` are generated from `TeleClient` by `scripts/gen_async.py`; CI and the
tests check that they match.

Integration tests run against a live Tele API (with LiveKit) when these are set:

```bash
export TELE_API_URL=http://127.0.0.1:8080 TELE_API_KEY=... TELE_API_SECRET=...
export TELE_ADMIN_API_KEY=... TELE_ADMIN_API_SECRET=...   # optional: tenant keys, Tele-Client, rates
uv run pytest tests/integration
```

## License

MIT. See [LICENSE](LICENSE).
