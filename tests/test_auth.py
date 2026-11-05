from __future__ import annotations

import json
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest

from shortform.auth import AuthManager, AuthRequired, TokenStore, callback_handler, validate_callback
from shortform.http import FakeTransport, Response


def test_token_store_is_private_atomic_and_uses_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORM_CONFIG_DIR", str(tmp_path / "config"))
    store = TokenStore()
    store.save("youtube", {"access_token": "secret", "account_id": "UC1"})
    path = tmp_path / "config" / "tokens" / "youtube.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.load("youtube")["account_id"] == "UC1"
    assert not list(path.parent.glob("*.tmp"))


def test_missing_credentials_fails_before_any_network_request(tmp_path):
    transport = FakeTransport()
    with pytest.raises(AuthRequired):
        AuthManager("youtube", transport=transport, store=TokenStore(tmp_path)).authorize()
    assert transport.calls == []


def test_state_validation_rejects_wrong_state():
    with pytest.raises(AuthRequired):
        validate_callback({"state": "wrong", "code": "code"}, "expected")


def test_loopback_handler_rejects_wrong_state_without_a_socket():
    sink: dict[str, str] = {}
    handler = object.__new__(callback_handler("expected", sink))
    statuses: list[int] = []
    handler.path = "/oauth/callback?state=wrong&code=code"
    handler.send_response = statuses.append
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()
    handler.do_GET()
    assert statuses == [400]
    assert "code" not in sink
    assert "state" in sink["error"]


def test_youtube_refresh_preserves_binding(tmp_path):
    (tmp_path / "credentials.json").write_text(json.dumps({"youtube": {"client_id": "id"}}))
    store = TokenStore(tmp_path)
    store.save("youtube", {"access_token": "old", "refresh_token": "refresh", "expires_at": 10, "account_id": "UC1"})
    transport = FakeTransport([Response(200, {}, b'{"access_token":"new","expires_in":3600}')])
    assert AuthManager("youtube", transport=transport, store=store, now=lambda: 20).access_token("UC1") == "new"
    assert "refresh_token=refresh" in transport.calls[0]["body"].decode()
    assert store.load("youtube")["refresh_token"] == "refresh"


def test_expired_token_refresh_rejection_is_auth_required(tmp_path):
    (tmp_path / "credentials.json").write_text(json.dumps({"youtube": {"client_id": "id"}}))
    store = TokenStore(tmp_path)
    store.save("youtube", {"access_token": "old", "refresh_token": "refresh", "expires_at": 10, "account_id": "UC1"})
    with pytest.raises(AuthRequired):
        AuthManager("youtube", transport=FakeTransport([Response(400, {}, b'{}')]), store=store, now=lambda: 20).access_token()


def test_account_binding_mismatch_fails_before_network_request(tmp_path):
    store = TokenStore(tmp_path)
    store.save("youtube", {"access_token": "token", "account_id": "UC-right", "expires_at": 99999})
    transport = FakeTransport()
    with pytest.raises(AuthRequired):
        AuthManager("youtube", transport=transport, store=store, now=lambda: 1).access_token("UC-wrong")
    assert transport.calls == []


def test_expired_instagram_token_requires_reauthorization(tmp_path):
    store = TokenStore(tmp_path)
    store.save("instagram", {"access_token": "page-token", "account_id": "ig", "expires_at": 10})
    transport = FakeTransport()
    with pytest.raises(AuthRequired, match="authenticate again"):
        AuthManager("instagram", transport=transport, store=store, now=lambda: 20).access_token("ig")
    assert transport.calls == []


def test_tiktok_manual_callback_checks_state_before_exchange(tmp_path):
    (tmp_path / "credentials.json").write_text(json.dumps({"tiktok": {"client_key": "key", "client_secret": "secret", "redirect_uri": "https://example.test/callback"}}))
    opened: list[str] = []
    def paste(_: str) -> str:
        state = parse_qs(urlparse(opened[0]).query)["state"][0]
        return f"https://example.test/callback?code=code&state={state}"
    transport = FakeTransport([Response(200, {}, b'{"access_token":"token","refresh_token":"refresh","expires_in":3600,"open_id":"open-id"}')])
    token = AuthManager("tiktok", transport=transport, store=TokenStore(tmp_path), browser_open=opened.append, input_func=paste).authorize()
    assert token["account_id"] == "open-id"
    assert "video.upload" in opened[0]


def test_manual_callback_requires_registered_redirect_before_exchange(tmp_path):
    (tmp_path / "credentials.json").write_text(json.dumps({"tiktok": {"client_key": "key", "client_secret": "secret", "redirect_uri": "https://example.test/callback"}}))
    opened: list[str] = []
    def wrong_redirect(_: str) -> str:
        state = parse_qs(urlparse(opened[0]).query)["state"][0]
        return f"https://attacker.test/callback?code=code&state={state}"
    transport = FakeTransport()
    with pytest.raises(AuthRequired, match="registered redirect"):
        AuthManager("tiktok", transport=transport, store=TokenStore(tmp_path), browser_open=opened.append, input_func=wrong_redirect).authorize()
    assert transport.calls == []


def test_tiktok_refresh_rejects_changed_account_binding(tmp_path):
    (tmp_path / "credentials.json").write_text(json.dumps({"tiktok": {"client_key": "key", "client_secret": "secret", "redirect_uri": "https://example.test/callback"}}))
    store = TokenStore(tmp_path)
    store.save("tiktok", {"access_token": "old", "refresh_token": "refresh", "account_id": "open-right", "expires_at": 10})
    transport = FakeTransport([Response(200, {}, b'{"access_token":"new","open_id":"open-wrong","expires_in":3600}')])
    with pytest.raises(AuthRequired, match="could not verify"):
        AuthManager("tiktok", transport=transport, store=store, now=lambda: 20).access_token("open-right")
