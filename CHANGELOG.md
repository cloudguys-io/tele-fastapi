# Changelog

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
- `tele_fastapi.fastapi`: `tele_token_router`, `tele_join_router` (invite and call joins, diagnostics),
  `TeleWebhook`, `tele_lifespan`, `get_tele` and `TeleClientDep`.
