from __future__ import annotations

import pytest

from shortform import pipeline
from shortform.publishers import MockPublisher

from .conftest import make_targets


def test_create_job_missing_video_raises(tmp_path):
    with pytest.raises(ValueError):
        pipeline.create_job(tmp_path / "nope.mp4", make_targets("mockA"))


def test_create_job_initializes_records_queued(video, jobs_dir):
    job = pipeline.create_job(video, make_targets("mockA", "mockB"))
    assert {p: r.state for p, r in job.records.items()} == {"mockA": "queued", "mockB": "queued"}
    assert (jobs_dir / f"{job.id}.json").exists()


def test_run_without_approval_raises(video):
    job = pipeline.create_job(video, make_targets("mockA"))
    pub = MockPublisher("mockA")
    with pytest.raises(RuntimeError, match="승인 필요 또는 승인 후 변경됨"):
        pipeline.run(job, {"mockA": pub})
    assert pub.publish_count == 0


def test_video_one_byte_change_invalidates_approval(video, approved_job):
    job = approved_job("mockA")
    assert pipeline.approval_valid(job)

    data = bytearray(video.read_bytes())
    data[-1] ^= 0x01
    video.write_bytes(bytes(data))

    assert pipeline.approval_valid(job) is False
    pub = MockPublisher("mockA")
    with pytest.raises(RuntimeError):
        pipeline.run(job, {"mockA": pub})
    assert pub.publish_count == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t["mockB"].metadata.__setitem__("caption", "caption-mockB!"),
        lambda t: setattr(t["mockA"], "privacy", "public"),
        lambda t: setattr(t["mockA"], "account_id", "other"),
        lambda t: setattr(t["mockA"], "provider", "other"),
        lambda t: t["mockA"].metadata.__setitem__("title", "T2"),
    ],
    ids=["caption", "privacy", "account", "provider", "title"],
)
def test_target_change_invalidates_approval(approved_job, mutate):
    job = approved_job("mockA", "mockB")
    mutate(job.targets)

    assert pipeline.approval_valid(job) is False
    pubs = {"mockA": MockPublisher("mockA"), "mockB": MockPublisher("mockB")}
    with pytest.raises(RuntimeError, match="승인 필요 또는 승인 후 변경됨"):
        pipeline.run(job, pubs)
    assert all(p.publish_count == 0 for p in pubs.values())


def test_approval_survives_reload(approved_job):
    job = approved_job("mockA")
    reloaded = pipeline.load_job(job.id)
    assert pipeline.approval_valid(reloaded)


def test_deleted_video_invalidates_approval(video, approved_job):
    job = approved_job("mockA")
    video.unlink()
    assert pipeline.approval_valid(job) is False
