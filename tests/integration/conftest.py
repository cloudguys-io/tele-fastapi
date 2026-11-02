"""Integration tests against a live Tele API. Skipped unless TELE_API_URL, TELE_API_KEY and TELE_API_SECRET are set.

TELE_ADMIN_API_KEY and TELE_ADMIN_API_SECRET enable the admin tests (tenant keys, Tele-Client, rates).
User ids and owner refs are prefixed with "tele-fastapi-" and a random suffix so runs never collide.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator

import pytest

from tele_fastapi import TeleClient, TeleSigner, TeleUser

REQUIRED = ("TELE_API_URL", "TELE_API_KEY", "TELE_API_SECRET")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    missing = [v for v in REQUIRED if not os.environ.get(v)]
    for item in items:
        if "tests/integration" in str(item.path).replace(os.sep, "/"):
            item.add_marker(pytest.mark.integration)
            if missing:
                item.add_marker(pytest.mark.skip(reason=f"live Tele API not configured ({', '.join(missing)} unset)"))


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ["TELE_API_URL"]


@pytest.fixture(scope="session")
def signer() -> TeleSigner:
    return TeleSigner(os.environ["TELE_API_KEY"], os.environ["TELE_API_SECRET"])


@pytest.fixture(scope="session")
def admin_signer() -> TeleSigner:
    key, secret = os.environ.get("TELE_ADMIN_API_KEY"), os.environ.get("TELE_ADMIN_API_SECRET")
    if not key or not secret:
        pytest.skip("TELE_ADMIN_API_KEY / TELE_ADMIN_API_SECRET unset")
    return TeleSigner(key, secret)


@pytest.fixture
def tele(signer: TeleSigner, base_url: str) -> Iterator[TeleClient]:
    with TeleClient(signer, base_url=base_url) as client:
        yield client


@pytest.fixture
def admin(admin_signer: TeleSigner, base_url: str) -> Iterator[TeleClient]:
    with TeleClient(admin_signer, base_url=base_url) as client:
        yield client


@pytest.fixture
def uid() -> str:
    """A run-unique prefix for user ids and owner refs."""
    return f"tele-fastapi-{secrets.token_hex(4)}"


def person(uid: str, name: str) -> TeleUser:
    return TeleUser(id=f"{uid}-{name.lower()}", name=name, team=f"{uid}-team")
