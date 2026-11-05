"""Offline CLI integration through real adapters, using only fake network responses."""
import json
from shortform import cli, pipeline
from shortform.auth import AuthRequired
from shortform.http import FakeTransport, Response
from shortform.publishers.youtube import YouTubePublisher
from shortform.publishers.instagram import InstagramPublisher
from shortform.publishers.tiktok import TikTokPublisher
from shortform.preflight import PreflightResult


class Auth:
    def __init__(self, reject=False):
        self.reject = reject
    def access_token(self, account_id=None):
        if self.reject:
            raise AuthRequired("authenticate again")
        return "fake-token"


def res(data, status=200, headers=None):
    return Response(status, headers or {}, json.dumps(data).encode())


def test_cli_one_expired_account_does_not_block_other_platforms(video, tmp_path, monkeypatch, capsys, jobs_dir):
    import shortform.preflight
    config = tmp_path / "shortform.toml"
    config.write_text('''[defaults]
title="test"
caption="paste this"
[platforms.youtube]
enabled=true
account_id="UC1"
[platforms.instagram]
enabled=true
account_id="ig1"
[platforms.tiktok]
enabled=true
account_id="tk1"
''')
    yt = YouTubePublisher(FakeTransport(), Auth(reject=True))
    ig = InstagramPublisher(FakeTransport([
        res({"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]}),
        res({"id": "container"}), res({"success": True}),
        res({"status_code": "FINISHED"}), res({"id": "media"}), res({"permalink": "https://example.org/reel"}),
    ]), Auth(), sleep=lambda _: None)
    tk = TikTokPublisher(FakeTransport([
        res({"data": {"publish_id": "pid", "upload_url": "https://upload.example.org/one"}}),
        res({}, 201), res({"data": {"status": "SEND_TO_USER_INBOX"}}),
    ]), Auth(), sleep=lambda _: None)
    monkeypatch.setattr(cli, "_publishers", lambda targets: {"youtube": yt, "instagram": ig, "tiktok": tk})
    monkeypatch.setattr(shortform.preflight, "check_video", lambda *args: PreflightResult())
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert cli.main(["run", str(video), "--config", str(config)]) == 1
    job = pipeline.load_job(next(jobs_dir.glob("*.json")).stem)
    assert job.records["youtube"].state == "failed"
    assert not yt.transport.calls
    assert job.records["instagram"].state == "published"
    assert job.records["tiktok"].delivery == "inbox"
    assert "paste this" in capsys.readouterr().out
    # Successful destinations are never called again on retry.
    ig_count, tk_count = len(ig.transport.calls), len(tk.transport.calls)
    assert cli.main(["retry", job.id]) == 1
    assert len(ig.transport.calls) == ig_count
    assert len(tk.transport.calls) == tk_count
