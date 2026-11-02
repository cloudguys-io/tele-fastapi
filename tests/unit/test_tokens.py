from __future__ import annotations

from datetime import timedelta

import jwt
import pytest

from tele_fastapi import MAX_SERVICE_TTL_SECONDS, MAX_USER_TTL_SECONDS, TeleSigner, TeleUser, TokenError

SECRET = "s" * 40
NOW = 1_760_000_000


def signer() -> TeleSigner:
    return TeleSigner("TK3f9QpX2mLk8VbN0aZr", SECRET, now=lambda: NOW + 0.9)


def decode(token: str) -> dict[str, object]:
    claims: dict[str, object] = jwt.decode(
        token, SECRET, algorithms=["HS256"], options={"verify_exp": False, "verify_nbf": False, "verify_iat": False}
    )
    return claims


def test_user_token_claims_match_the_guide() -> None:
    tok = signer().user_token(TeleUser(id="user-123", name="Rahim Uddin", team="sales"), 900)
    assert jwt.get_unverified_header(tok) == {"alg": "HS256", "typ": "JWT"}
    assert decode(tok) == {
        "iss": "TK3f9QpX2mLk8VbN0aZr",
        "sub": "user-123",
        "iat": NOW,
        "nbf": NOW,
        "exp": NOW + 900,
        "name": "Rahim Uddin",
        "team": "sales",
        "tele": {"scope": "user"},
    }


def test_user_token_omits_empty_name_and_team() -> None:
    claims = decode(signer().user_token(TeleUser(id="u")))
    assert "name" not in claims and "team" not in claims
    assert claims["exp"] == NOW + 900  # default ttl


def test_service_token_claims() -> None:
    claims = decode(signer().service_token())
    assert claims == {
        "iss": "TK3f9QpX2mLk8VbN0aZr",
        "iat": NOW,
        "nbf": NOW,
        "exp": NOW + 300,
        "tele": {"scope": "service"},
    }


def test_ttl_accepts_timedelta_and_limits() -> None:
    s = signer()
    assert decode(s.user_token(TeleUser("u"), timedelta(hours=1)))["exp"] == NOW + MAX_USER_TTL_SECONDS
    assert decode(s.service_token(timedelta(minutes=10)))["exp"] == NOW + MAX_SERVICE_TTL_SECONDS
    with pytest.raises(TokenError, match=r"user token ttl must be in \(0, 3600\]"):
        s.user_token(TeleUser("u"), 3601)
    with pytest.raises(TokenError, match=r"service token ttl must be in \(0, 600\]"):
        s.service_token(601)
    with pytest.raises(TokenError):
        s.service_token(0)
    with pytest.raises(TokenError):
        s.user_token(TeleUser("u"), -5)


@pytest.mark.parametrize(
    ("user", "message"),
    [
        (TeleUser(id=""), "user.id is required"),
        (TeleUser(id="x" * 129), "longer than 128"),
        (TeleUser(id="u", name="n" * 65), "limited to 64"),
        (TeleUser(id="u", team="t" * 65), "limited to 64"),
    ],
)
def test_user_validation(user: TeleUser, message: str) -> None:
    with pytest.raises(TokenError, match=message):
        signer().user_token(user)


def test_limits_count_characters_not_bytes() -> None:
    tok = signer().user_token(TeleUser(id="ব" * 128, name="ব" * 64, team="👋" * 64))
    assert decode(tok)["sub"] == "ব" * 128


def test_signer_requires_key_and_secret_and_hides_secret() -> None:
    with pytest.raises(TokenError, match="api_key and api_secret are required"):
        TeleSigner("", SECRET)
    with pytest.raises(TokenError):
        TeleSigner("k", "")
    assert SECRET not in repr(signer())
