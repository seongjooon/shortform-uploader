import pytest
from shortform import pipeline
from shortform.http import FakeTransport, ConnectError
from shortform.publishers.base import TimeoutAfterAccept, TransportError
from shortform.publishers.youtube import YouTubePublisher
from shortform.publishers.instagram import InstagramPublisher
from shortform.publishers.tiktok import TikTokPublisher, MiB
from tests.test_adapters import FakeAuth, response


@pytest.mark.parametrize("size", [1, 3*MiB, 5*MiB, 7*MiB, 10*MiB, 23*MiB, 26*MiB, 4*1024**3])
def test_tiktok_chunk_arithmetic(size):
    plan = TikTokPublisher.chunk_plan(size)
    assert plan[0][0] == 0
    assert sum(length for _, length in plan) == size
    assert size // plan[0][1] == len(plan)
    for (offset, length), (next_offset, _) in zip(plan, plan[1:]):
        assert offset + length == next_offset
    assert plan[-1][1] <= 128*MiB


def ig_prefix():
    return [response(200, {"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]}),
            response(200, {"id": "container"}), response(200)]


def test_ig_permalink_rejection_still_success(video):
    transport = FakeTransport(ig_prefix() + [response(200, {"status_code": "FINISHED"}),
        response(200, {"id": "posted-media"}), response(401)])
    result = InstagramPublisher(transport, FakeAuth(), sleep=lambda _: None).publish(str(video), {"account_id": "ig"})
    assert result.remote_id == "posted-media"
    assert result.url is None


def test_ig_status_unauthorized_is_unknown(video):
    transport = FakeTransport(ig_prefix() + [response(401)])
    with pytest.raises(TimeoutAfterAccept):
        InstagramPublisher(transport, FakeAuth(), sleep=lambda _: None).publish(str(video), {"account_id": "ig"})
    assert not any("media_publish" in call["url"] for call in transport.calls)


def test_ig_publish_timeout_reconciles_without_republish(approved_job):
    job = approved_job("instagram")
    transport = FakeTransport(ig_prefix() + [response(200, {"status_code": "FINISHED"}),
        TransportError("lost"), response(200, {"status_code": "PUBLISHED"})])
    pub = InstagramPublisher(transport, FakeAuth(), sleep=lambda _: None)
    pipeline.run(job, {"instagram": pub})
    assert job.records["instagram"].state == "unknown"
    pipeline.retry(job, {"instagram": pub})
    assert job.records["instagram"].state == "published"
    assert sum("media_publish" in c["url"] for c in transport.calls) == 1


def test_youtube_unknown_session_never_resubmits(approved_job):
    job = approved_job("youtube")
    transport = FakeTransport([response(200, headers={"Location": "https://upload.example/session"}),
        TransportError("lost"), ConnectError("status lookup lost"), response(404)])
    pub = YouTubePublisher(transport, FakeAuth())
    pipeline.run(job, {"youtube": pub})
    assert job.records["youtube"].state == "unknown"
    pipeline.retry(job, {"youtube": pub})
    assert job.records["youtube"].needs_manual
    assert sum(c["method"] == "POST" for c in transport.calls) == 1


def test_tiktok_failed_spam_blocks_resubmission(video):
    from shortform.models import PlatformTarget
    job = pipeline.create_job(video, {"tiktok": PlatformTarget("tiktok", "me", "inbox", {})})
    pipeline.approve(job)
    transport = FakeTransport([response(200, {"data": {"publish_id": "pid", "upload_url": "https://upload.example/video"}}),
        response(201), response(200, {"data": {"status": "FAILED", "fail_reason": "spam_risk"}}),
        response(200, {"data": {"status": "FAILED", "fail_reason": "spam_risk"}})])
    pub = TikTokPublisher(transport, FakeAuth(), sleep=lambda _: None)
    pipeline.run(job, {"tiktok": pub})
    pipeline.retry(job, {"tiktok": pub})
    assert job.records["tiktok"].state == "failed"
    assert job.records["tiktok"].needs_manual
    count = len(transport.calls)
    pipeline.retry(job, {"tiktok": pub})
    assert len(transport.calls) == count


def test_tiktok_initial_complete_uses_status_post_id(video):
    transport = FakeTransport([response(200, {"data": {"publish_id": "pid", "upload_url": "https://upload.example/video"}}),
        response(201), response(200, {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [123]}})])
    result = TikTokPublisher(transport, FakeAuth(), sleep=lambda _: None).publish(str(video), {"account_id": "tk"})
    assert result.remote_id == "123"
    assert result.delivery == "posted"
