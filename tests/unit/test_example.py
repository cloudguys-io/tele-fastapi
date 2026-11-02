from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "app.py"


def test_example_app_builds_with_every_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELE_API_KEY", "TKexample")
    monkeypatch.setenv("TELE_API_SECRET", "e" * 40)
    monkeypatch.setenv("TELE_WEBHOOK_SECRET", "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw")
    spec = importlib.util.spec_from_file_location("tele_example_app", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = module.app.openapi()["paths"]
    routes = {(method.upper(), path) for path, ops in paths.items() for method in ops}
    assert {
        ("POST", "/api/tele/token"),
        ("POST", "/api/tele/invites/{code}/join"),
        ("POST", "/api/tele/calls/{call_id}/join"),
        ("POST", "/api/tele/calls/{call_id}/diagnostics"),
        ("GET", "/api/tele/invites/{code}"),
        ("GET", "/api/tele/calls/{call_id}/messages"),
        ("POST", "/api/calls"),
        ("GET", "/api/calls"),
        ("POST", "/api/calls/{call_id}/end"),
        ("GET", "/api/recordings/{recording_id}/download"),
        ("POST", "/api/tele/webhooks"),
    } <= routes
