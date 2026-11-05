import json
import threading
import time

import pytest

from shortform.gui.server import App, make_handler
from shortform.publishers.mock import MockPublisher


class Store:
    def load(self, platform):
        return {"account_id": "test-account"} if platform == "youtube" else None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTFORM_JOBS_DIR", str(tmp_path / "jobs"))
    app = App(directory=tmp_path / "videos", store=Store(),
              publishers_factory=lambda targets: {p: MockPublisher(p) for p in targets})
    import io
    from types import SimpleNamespace
    host = "127.0.0.1:12345"
    def request(method, path, body=None, headers=None):
        body = body.encode() if isinstance(body, str) else body or b""
        raw_headers = {"Host": host, "Content-Length": str(len(body)), **(headers or {})}
        request_bytes = (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in raw_headers.items()) + "\r\n").encode() + body
        class Connection:
            def makefile(self, mode, *args, **kwargs):
                return io.BytesIO(request_bytes) if mode == "rb" else output
            def sendall(self, data):
                output.write(data)
        class Output(io.BytesIO):
            def close(self):
                pass
        output = Output()
        make_handler(app)(Connection(), ("127.0.0.1", 1), SimpleNamespace(server_port=12345))
        head, content = output.getvalue().split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        response_headers = dict(line.split(": ", 1) for line in lines[1:])
        return int(lines[0].split()[1]), response_headers, content
    yield app, host, request


def test_security_checks(client):
    app, host, request = client
    assert request("GET", "/api/accounts")[0] == 403
    assert request("GET", "/?t=" + app.token, headers={"Host": "evil.test"})[0] == 403
    status, headers, _ = request("GET", "/?t=" + app.token)
    assert status == 303 and "HttpOnly" in headers["Set-Cookie"]
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    assert request("POST", "/api/plans", b"{}", {"Cookie": cookie, "Origin": "http://evil.test"})[0] == 403
    assert request("GET", "/api/accounts", headers={"Cookie": cookie})[0] == 200


def test_upload_flow_and_single_use_confirmation(client, monkeypatch):
    from shortform import preflight
    monkeypatch.setattr(preflight, "_probe", lambda p: ({}, None))
    app, host, request = client
    headers = {"Cookie": "shortform_session=" + app.token, "Origin": "http://" + host}
    status, _, raw = request("POST", "/api/videos", b"fake mp4", headers)
    assert status == 200
    video = json.loads(raw)
    data = {"video_id": video["id"], "caption": "hello", "hashtags": "#tag", "title": "", "platforms": ["youtube"]}
    status, _, raw = request("POST", "/api/plans", json.dumps(data), headers)
    assert status == 200
    plan_id = json.loads(raw)["plan_id"]
    assert request("POST", "/api/uploads", json.dumps({"plan_id": plan_id}), headers)[0] == 400
    payload = json.dumps({"plan_id": plan_id, "confirmed": True})
    status, _, raw = request("POST", "/api/uploads", payload, headers)
    assert status == 202
    upload_id = json.loads(raw)["id"]
    assert request("POST", "/api/uploads", payload, headers)[0] == 400
    for _ in range(100):
        status, _, raw = request("GET", "/api/uploads/" + upload_id, headers={"Cookie": headers["Cookie"]})
        result = json.loads(raw)
        if result["done"]:
            break
        time.sleep(.01)
    assert result["done"] and result["platforms"]["youtube"]["state"] == "published"


def test_preflight_errors_block_plan(client, monkeypatch):
    from shortform import preflight
    monkeypatch.setattr(preflight, "_probe", lambda p: ({"format": {"duration": "181"}}, None))
    app, host, request = client
    headers = {"Cookie": "shortform_session=" + app.token, "Origin": "http://" + host}
    _, _, raw = request("POST", "/api/videos", b"video", headers)
    video = json.loads(raw)
    assert video["checks"]["errors"]["youtube"]
    data = {"video_id": video["id"], "caption": "", "hashtags": "", "title": "", "platforms": ["youtube"]}
    assert request("POST", "/api/plans", json.dumps(data), headers)[0] == 400


def test_setup_steps_and_auth_flow(client, tmp_path, monkeypatch):
    from shortform import service
    from shortform.auth import TokenStore
    from shortform.setup import Progress
    app, host, request = client
    app.store = TokenStore(tmp_path / 'auth')
    headers = {"Cookie": "shortform_session=" + app.token, "Origin": "http://" + host}
    status, _, raw = request("GET", "/api/setup/tiktok/steps", headers=headers)
    assert status == 200 and json.loads(raw)["steps"]
    def post(action, data):
        return request("POST", "/api/setup/tiktok/" + action, json.dumps(data), headers)
    assert post("step", {"step": "client_key", "value": "bad"})[0] == 400
    assert "client_key" not in Progress(app.store).done_steps("tiktok")
    Progress(app.store).save_creds("tiktok", {"redirect_uri": "https://example.com/callback", "client_key": "private-key", "client_secret": "private-secret"})
    def connect(platform, *, store, browser_open, input_func):
        browser_open("https://example.com/authorize")
        assert input_func("url") == "https://example.com/callback?code=test"
        store.save(platform, {"account_id": "account", "access_token": "private-token"})
    monkeypatch.setattr(service, "connect", connect)
    assert post("start", {})[0] == 202
    for _ in range(100):
        _, _, raw = request("GET", "/api/setup/tiktok/auth", headers=headers)
        state = json.loads(raw)
        if state["state"] == "callback":
            break
        time.sleep(.01)
    assert state["state"] == "callback"
    assert post("start", {})[0] == 400
    assert post("callback", {"url": "https://example.com/callback?code=test"})[0] == 200
    for _ in range(100):
        _, _, raw = request("GET", "/api/setup/tiktok/auth", headers=headers)
        if json.loads(raw)["state"] == "done":
            break
        time.sleep(.01)
    assert json.loads(raw)["state"] == "done"
    _, _, steps = request("GET", "/api/setup/tiktok/steps", headers=headers)
    assert b"private-key" not in steps and b"private-secret" not in steps and b"private-token" not in steps
    assert b"private-token" not in raw
