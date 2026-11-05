"""Socket-free contracts for the three live publisher adapters."""

from __future__ import annotations

import json

from shortform.http import FakeTransport, Response
from shortform.publishers.base import DefinitiveRejection, ServerError
from shortform.publishers.instagram import InstagramPublisher
from shortform.publishers.tiktok import MiB, TikTokPublisher
from shortform.publishers.youtube import YouTubePublisher


class FakeAuth:
    def __init__(self) -> None:
        self.accounts: list[str | None] = []

    def access_token(self, account_id=None):
        self.accounts.append(account_id)
        return "test-token"


def response(status: int, payload=None, headers=None) -> Response:
    return Response(status, headers or {}, json.dumps(payload or {}).encode())


def test_youtube_starts_resumable_session_and_publishes(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    transport = FakeTransport([
        response(200, headers={"Location": "https://upload.example/session"}),
        response(201, {"id": "yt-id"}),
    ])
    handles: list[str] = []
    result = YouTubePublisher(transport, FakeAuth()).publish(
        str(video), {"account_id": "UC1", "privacy": "private", "metadata": {"title": "T"}}, on_handle=handles.append
    )
    assert result.remote_id == "yt-id"
    assert result.url == "https://www.youtube.com/shorts/yt-id"
    start = transport.calls[0]
    assert json.loads(start["body"])["status"]["privacyStatus"] == "private"
    assert start["headers"]["X-Upload-Content-Length"] == "5"
    assert handles[0].startswith("yt1:")


def test_youtube_resumes_from_range_after_interrupted_put(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"abcdefghij")
    transport = FakeTransport([
        response(200, headers={"Location": "https://upload.example/session"}),
        response(308, headers={"Range": "bytes=0-4"}),
        response(201, {"id": "yt-id"}),
    ])
    YouTubePublisher(transport, FakeAuth()).publish(str(video), {"account_id": "UC1", "metadata": {}})
    assert transport.calls[2]["headers"]["Content-Range"] == "bytes 5-9/10"


def test_instagram_uploads_resumable_reel_then_publishes(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    transport = FakeTransport([
        response(200, {"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]}),
        response(200, {"id": "container"}),
        response(200),
        response(200, {"status_code": "FINISHED"}),
        response(200, {"id": "media"}),
        response(200, {"permalink": "https://instagram.example/reel"}),
    ])
    result = InstagramPublisher(transport, FakeAuth(), sleep=lambda _: None).publish(
        str(video), {"account_id": "ig1", "metadata": {"caption": "caption"}}
    )
    assert result.remote_id == "media"
    assert result.delivery == "posted"
    upload = transport.calls[2]
    assert upload["headers"]["offset"] == "0"
    assert upload["headers"]["file_size"] == "5"


def test_tiktok_chunk_plan_merges_small_final_fragment():
    assert TikTokPublisher.chunk_plan(3 * MiB) == [(0, 3 * MiB)]
    assert TikTokPublisher.chunk_plan(23 * MiB) == [(0, 10 * MiB), (10 * MiB, 13 * MiB)]
    assert TikTokPublisher.chunk_plan(26 * MiB) == [(0, 10 * MiB), (10 * MiB, 16 * MiB)]


def test_tiktok_inbox_upload_never_sends_caption(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    transport = FakeTransport([
        response(200, {"data": {"publish_id": "pid", "upload_url": "https://upload.example/one"}, "error": {"code": "ok"}}),
        response(201),
        response(200, {"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}}),
    ])
    result = TikTokPublisher(transport, FakeAuth(), sleep=lambda _: None).publish(
        str(video), {"account_id": "open-id", "metadata": {"caption": "never upload this"}}
    )
    assert result.delivery == "inbox"
    assert b"caption" not in transport.calls[0]["body"]
    assert transport.calls[1]["headers"]["Content-Range"] == "bytes 0-4/5"


def test_instagram_does_not_create_container_when_quota_is_unverifiable(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    transport = FakeTransport([response(200, {"data": [{"quota_usage": 1}]})])
    try:
        InstagramPublisher(transport, FakeAuth()).publish(str(video), {"account_id": "ig1", "metadata": {}})
    except ServerError:
        pass
    else:
        raise AssertionError("expected unverified quota to stop the upload")
    assert len(transport.calls) == 1


def test_tiktok_completed_inbox_post_uses_first_public_post_id():
    publisher = TikTokPublisher(FakeTransport([
        response(200, {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [123]}, "error": {"code": "ok"}})
    ]), FakeAuth())
    result = publisher.reconcile("pid")
    assert result.remote_id == "123"
    assert result.delivery == "posted"


def _direct_transport(options, *, status="PUBLISH_COMPLETE"):
    return FakeTransport([
        response(200, {"data": {"privacy_level_options": options}, "error": {"code": "ok"}}),
        response(200, {"data": {"publish_id": "pid", "upload_url": "https://upload.example/one"}, "error": {"code": "ok"}}),
        response(201),
        response(200, {"data": {"status": status}, "error": {"code": "ok"}}),
    ])


def test_tiktok_direct_post_sends_caption_and_requested_privacy(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    transport = _direct_transport(["PUBLIC_TO_EVERYONE", "SELF_ONLY"])
    result = TikTokPublisher(transport, FakeAuth(), sleep=lambda _: None).publish(
        str(video), {"account_id": "open-id", "privacy": "public", "metadata": {"caption": "오늘 러닝 #run"}}
    )
    init = json.loads(transport.calls[1]["body"])
    assert transport.calls[1]["url"].endswith("/v2/post/publish/video/init/")
    assert init["post_info"] == {"title": "오늘 러닝 #run", "privacy_level": "PUBLIC_TO_EVERYONE"}
    assert result.delivery == "posted" and result.note is None


def test_tiktok_direct_post_falls_back_to_more_private_level_with_note(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    transport = _direct_transport(["SELF_ONLY"])
    result = TikTokPublisher(transport, FakeAuth(), sleep=lambda _: None).publish(
        str(video), {"account_id": "open-id", "privacy": "public", "metadata": {"caption": "c"}}
    )
    assert json.loads(transport.calls[1]["body"])["post_info"]["privacy_level"] == "SELF_ONLY"
    assert "나만 보기" in result.note


def test_tiktok_direct_post_explains_unaudited_private_account_rule(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    transport = FakeTransport([
        response(200, {"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}}),
        response(200, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts"}}),
    ])
    try:
        TikTokPublisher(transport, FakeAuth(), sleep=lambda _: None).publish(
            str(video), {"account_id": "open-id", "privacy": "private", "metadata": {}})
    except DefinitiveRejection as exc:
        assert "비공개 계정" in str(exc)
    else:
        raise AssertionError("expected a definitive rejection")


def test_tiktok_direct_post_403_surfaces_error_code_only(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    transport = FakeTransport([
        response(200, {"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE"]}, "error": {"code": "ok"}}),
        response(403, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "secret echo"}}),
    ])
    try:
        TikTokPublisher(transport, FakeAuth(), sleep=lambda _: None).publish(
            str(video), {"account_id": "open-id", "privacy": "public", "metadata": {}})
    except DefinitiveRejection as exc:
        assert "비공개 계정" in str(exc) and "secret echo" not in str(exc)
    else:
        raise AssertionError("expected rejection")
