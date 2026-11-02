# Changelog

## 0.2.0 (2026-11-03)

Follows the Tele server change that lets a service token join through an invite.

- `join_invite(code, user=JoinUser(...))` joins through an invite on a user's behalf with a service token, sync and
  async. Tele records the user's IP from the body.
- `tele_join_router`: the invite join is now a service-token join carrying the user's IP and user agent, like the
  call join; `service_join=True` (the default) covers both routes.
- List limits are checked before the request, matching the server's range (a `ValueError` otherwise):
  `list_calls` 1-200, `list_messages` 1-500, `list_webhook_deliveries` 1-200.
- Docs: removed the "backend IP" and "no CORS" caveats. The Tele API allows any origin, so browsers may call it
  directly with a user token; user IDs containing `/` work in member paths.

## 0.1.0 (2026-11-03)

First release.

- `TeleSigner`: user tokens (≤ 1 h) and service tokens (≤ 10 min) with the same claims, limits and validation as
  `teletoken` (Go) and `@tele/node`.
- `TeleClient` and `AsyncTeleClient` for every endpoint in the Tele guide: calls, members, sessions, invites and previews,
  joins (user token, or service token on a user's behalf, with `X-Client-Platform`), chat, recordings (including
  streaming downloads with `Range` and restore), diagnostics, webhooks (CRUD, rotate, test, deliveries, retry,
  `Tele-Client` for admin keys), tenant API keys, usage (JSON and CSV), rates and `me`. Service tokens are minted
  and cached automatically; `.as_user()` acts as one of your users.
- Typed pydantic v2 models and `TeleError` subclasses with `code`, `message`, `opens_at` and `request_id`.
- `tele_fastapi.webhooks`: Standard Webhooks verification (5 minute tolerance, several signatures during a secret
  rotation) and typed models for every event type, with an `UnknownEvent` fallback.
- `tele_fastapi.fastapi`: the shared backend routes under `/api/tele` (`tele_token_router` for `POST /token`;
  `tele_join_router` for the invite preview and join, call join, chat history and diagnostics), errors in Tele's
  shape (`TeleErrorRoute`, `tele_error_response`), `TeleWebhook`, `tele_lifespan`, `get_tele` and
  `TeleClientDep`.
