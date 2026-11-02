"""Client behaviour beyond single endpoints: credentials, derived clients, errors, streaming, validation."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx
import jwt
import pytest

from tele_fastapi import (
    DEFAULT_BASE_URL,
    AsyncTeleClient,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    GoneError,
    NotFoundError,
    NotImplementedByServerError,
    PermissionDeniedError,
    RateLimitedError,
    ServerError,
    TeleClient,
    TeleError,
    TeleSigner,
    TeleUser,
)

SECRET = "x" * 40


class Clock:
    def __init__(self) -> None:
        self.t = 1_760_000_000.0

    def __call__(self) -> float:
        return self.t


def claims(request: httpx.Request) -> dict[str, object]:
    token = request.headers["authorization"].removeprefix("Bearer ")
    out: dict[str, object] = jwt.decode(token, options={"verify_signature": False})
    return out


def recorder(status: int = 200, json: object = None, **kw: object) -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=json if json is not None else {"client": "c", "scope": "service"}, **kw)  # type: ignore[arg-type]

    return seen, httpx.MockTransport(handle)


def test_signer_client_mints_and_caches_service_tokens() -> None:
    clock = Clock()
    seen, transport = recorder()
    tele = TeleClient(
        TeleSigner("TK1", SECRET, now=clock), base_url="https://t", http_client=httpx.Client(transport=transport)
    )
    tele.me()
    tele.me()
    first, second = claims(seen[0]), claims(seen[1])
    assert first == second and first["tele"] == {"scope": "service"} and first["exp"] == first["iat"] + 300  # type: ignore[operator]
    clock.t += 241  # within 60 s of expiry: mint a fresh one
    tele.me()
    assert claims(seen[2])["iat"] == int(clock.t)


def test_as_user_with_token_and_for_tenant_share_the_pool() -> None:
    seen, transport = recorder()
    http = httpx.Client(transport=transport)
    tele = TeleClient(TeleSigner("TK1", SECRET), base_url="https://t", http_client=http)
    tele.as_user(TeleUser("u-1", "Rahim", "sales"), ttl=600).me()
    c = claims(seen[0])
    assert (c["sub"], c["name"], c["team"], c["tele"]) == ("u-1", "Rahim", "sales", {"scope": "user"})
    assert c["exp"] == c["iat"] + 600  # type: ignore[operator]
    tele.with_token("static").me()
    assert seen[1].headers["authorization"] == "Bearer static"
    tele.for_tenant("client-uuid").me()
    assert seen[2].headers["tele-client"] == "client-uuid"
    assert "tele-client" not in seen[0].headers
    assert seen[0].headers["user-agent"].startswith("tele-fastapi/")
    tele.as_user(TeleUser("u")).close()  # a derived client never closes the shared pool
    tele.me()
    assert len(seen) == 4


def test_as_user_needs_a_signer_and_validates_user() -> None:
    with pytest.raises(ValueError, match="TeleSigner"):
        TeleClient(token="t").as_user(TeleUser("u"))
    with pytest.raises(ValueError, match="128"):
        TeleClient(TeleSigner("k", SECRET)).as_user(TeleUser("u" * 129))


def test_constructor_validation_and_defaults() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        TeleClient()
    with pytest.raises(ValueError, match="exactly one"):
        AsyncTeleClient(TeleSigner("k", SECRET), token="t")
    with pytest.raises(ValueError, match="empty"):
        TeleClient(token="")
    tele = TeleClient(token="t")
    assert tele.base_url == DEFAULT_BASE_URL == "https://tele-api.cloudguys.io"
    assert tele.token() == "t"
    tele.close()


def test_naive_datetimes_and_empty_path_params_are_rejected() -> None:
    tele = TeleClient(token="t", http_client=httpx.Client(transport=recorder()[1]))
    with pytest.raises(ValueError, match="timezone-aware"):
        tele.create_invite("c", expires_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        tele.create_call("x", invite={"expires_at": datetime(2026, 1, 1)})
    with pytest.raises(ValueError, match="empty"):
        tele.get_call("")


@pytest.mark.parametrize(
    ("status", "code", "cls"),
    [
        (400, "invalid_request", BadRequestError),
        (401, "unauthenticated", AuthenticationError),
        (403, "not_invited", PermissionDeniedError),
        (404, "invite_not_found", NotFoundError),
        (409, "call_full", ConflictError),
        (410, "invite_exhausted", GoneError),
        (429, "rate_limited", RateLimitedError),
        (500, "internal", ServerError),
        (501, "recording_disabled", NotImplementedByServerError),
        (503, "not_ready", ServerError),
        (418, "teapot", TeleError),
    ],
)
def test_errors_map_to_subclasses(status: int, code: str, cls: type[TeleError]) -> None:
    _, transport = recorder(status, {"error": {"code": code, "message": "nope"}}, headers={"X-Request-Id": "req-7"})
    tele = TeleClient(token="t", http_client=httpx.Client(transport=transport))
    with pytest.raises(cls) as info:
        tele.get_call("c")
    err = info.value
    assert type(err) is cls
    assert (err.status, err.code, err.message, err.request_id) == (status, code, "nope", "req-7")
    assert err.retryable == (status == 429 or status >= 500)


def test_too_early_carries_opens_at_and_helpers() -> None:
    body = {"error": {"code": "too_early", "message": "not open", "opens_at": "2026-10-20T03:50:00.5Z"}}
    _, transport = recorder(409, body)
    tele = TeleClient(token="t", http_client=httpx.Client(transport=transport))
    with pytest.raises(ConflictError) as info:
        tele.join_invite("code")
    assert info.value.opens_at is not None and info.value.opens_at.minute == 50
    assert info.value.is_join_window and not info.value.is_invite_invalid and not info.value.is_call_closed
    assert info.value.body == body


def test_non_json_error_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>bad gateway</html>")

    tele = TeleClient(token="t", http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(ServerError) as info:
        tele.me()
    assert info.value.code == "http_502" and "bad gateway" in info.value.message


def test_raw_request_does_not_raise() -> None:
    _, transport = recorder(404, {"error": {"code": "not_found", "message": "x"}})
    tele = TeleClient(token="t", http_client=httpx.Client(transport=transport))
    assert tele.raw_request("GET", "/v1/calls/x").status_code == 404


VIDEO = bytes(range(256)) * 10


def video_handler(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/missing/download"):
            return httpx.Response(409, json={"error": {"code": "archived", "message": "restore first"}})
        rng = request.headers.get("range")
        if rng:
            start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
            part = VIDEO[start : end + 1]
            return httpx.Response(
                206,
                content=part,
                headers={
                    "content-type": "video/mp4",
                    "content-range": f"bytes {start}-{end}/{len(VIDEO)}",
                    "content-length": str(len(part)),
                    "accept-ranges": "bytes",
                    "etag": '"e1"',
                },
            )
        return httpx.Response(
            200, content=VIDEO, headers={"content-type": "video/mp4", "content-length": str(len(VIDEO))}
        )

    return httpx.MockTransport(handle)


def test_stream_recording_sync_with_range() -> None:
    seen: list[httpx.Request] = []
    tele = TeleClient(token="t", base_url="https://t", http_client=httpx.Client(transport=video_handler(seen)))
    with tele.stream_recording("rec-1", range="bytes=10-19") as video:
        assert video.partial and video.status_code == 206
        assert video.content_range == "bytes 10-19/2560" and video.content_length == 10 and video.etag == '"e1"'
        assert b"".join(video.iter_bytes(4)) == VIDEO[10:20]
        assert video.passthrough_headers()["Cache-Control"] == "private, no-store"
        assert video.passthrough_headers()["content-range"] == "bytes 10-19/2560"
    assert seen[0].url.path == "/v1/recordings/rec-1/download"
    assert seen[0].headers["range"] == "bytes=10-19" and seen[0].headers["authorization"] == "Bearer t"
    assert tele.download_recording("rec-1") == VIDEO
    with pytest.raises(ConflictError, match="archived"):
        tele.download_recording("missing")


def test_stream_recording_async() -> None:
    seen: list[httpx.Request] = []

    async def run() -> None:
        http = httpx.AsyncClient(transport=video_handler(seen))
        async with AsyncTeleClient(token="t", base_url="https://t", http_client=http) as tele:
            async with tele.stream_recording("rec-1", range="bytes=0-3") as video:
                assert video.partial and video.content_type == "video/mp4"
                chunks = [c async for c in video.aiter_bytes(2)]
            assert b"".join(chunks) == VIDEO[:4]
            assert await tele.download_recording("rec-1") == VIDEO
            with pytest.raises(ConflictError):
                await tele.download_recording("missing")
            assert (await tele.raw_request("GET", "/v1/recordings/missing/download")).status_code == 409

    asyncio.run(run())


def test_async_signer_client_and_derived_clients() -> None:
    seen, transport = recorder()

    async def run() -> None:
        tele = AsyncTeleClient(
            TeleSigner("TK1", SECRET), base_url="https://t", http_client=httpx.AsyncClient(transport=transport)
        )
        await tele.me()
        await tele.as_user(TeleUser("u-2")).me()
        await tele.for_tenant("cid").with_token("tok").me()
        await tele.aclose()

    asyncio.run(run())
    assert claims(seen[0])["tele"] == {"scope": "service"}
    assert claims(seen[1])["sub"] == "u-2"
    assert seen[2].headers["authorization"] == "Bearer tok" and seen[2].headers["tele-client"] == "cid"


def test_async_client_is_generated_from_sync() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/gen_async.py", "--check"], cwd=root, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_core_imports_without_fastapi() -> None:
    code = (
        "import sys; sys.modules['fastapi'] = None\n"
        "import tele_fastapi\n"
        "from tele_fastapi import TeleClient, AsyncTeleClient, TeleSigner, verify_webhook\n"
        "assert 'starlette' not in sys.modules\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    blocked = "import sys; sys.modules['fastapi'] = None\nimport tele_fastapi.fastapi\n"
    result = subprocess.run([sys.executable, "-c", blocked], capture_output=True, text=True)
    assert "pip install" in result.stderr
