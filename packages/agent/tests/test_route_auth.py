from __future__ import annotations

from aiask_agent import route_auth


def test_token_matches_uses_constant_time_compare(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_compare_digest(provided: str, expected: str) -> bool:
        calls.append((provided, expected))
        return provided == expected

    monkeypatch.setattr(route_auth.hmac, "compare_digest", fake_compare_digest)

    assert route_auth.token_matches("secret", "secret") is True
    assert route_auth.token_matches("wrong", "secret") is False
    assert route_auth.token_matches("", "secret") is False

    assert calls == [("secret", "secret"), ("wrong", "secret")]
