from __future__ import annotations

import json

import pytest

from shortform import pipeline
from shortform.publishers import MockPublisher, SimulatedCrash


def _disk(jobs_dir, job_id):
    return json.loads((jobs_dir / f"{job_id}.json").read_text(encoding="utf-8"))


def test_happy_path_publishes_each_platform_once(approved_job, jobs_dir):
    job = approved_job("mockA", "mockB")
    pubs = {"mockA": MockPublisher("mockA"), "mockB": MockPublisher("mockB")}

    pipeline.run(job, pubs)

    assert pubs["mockA"].publish_count == 1
    assert pubs["mockB"].publish_count == 1
    disk = _disk(jobs_dir, job.id)
    for p in ("mockA", "mockB"):
        rec = disk["records"][p]
        assert rec["state"] == "published"
        assert rec["url"].startswith(f"https://mock.invalid/{p}/")
        assert rec["remote_id"]
        assert rec["attempts"] == 1


def test_publisher_receives_target_payload(approved_job):
    job = approved_job("mockA")
    pub = MockPublisher("mockA")
    pipeline.run(job, {"mockA": pub})
    call = pub.calls[0]
    assert call.video_path == job.video_path
    assert call.target == {
        "account_id": "me",
        "privacy": "private",
        "metadata": {"title": "T", "caption": "caption-mockA", "description": "D"},
    }


def test_one_platform_failing_does_not_block_other(approved_job, jobs_dir):
    job = approved_job("mockA", "mockB")
    pubs = {"mockA": MockPublisher("mockA", "fail"), "mockB": MockPublisher("mockB")}

    pipeline.run(job, pubs)

    disk = _disk(jobs_dir, job.id)
    assert disk["records"]["mockA"]["state"] == "failed"
    assert disk["records"]["mockA"]["error"]
    assert disk["records"]["mockB"]["state"] == "published"


@pytest.mark.parametrize("scenario", ["unexpected_error", "server_error"])
def test_exception_in_first_platform_still_runs_second(approved_job, jobs_dir, scenario):
    job = approved_job("mockA", "mockB")
    pubs = {"mockA": MockPublisher("mockA", scenario), "mockB": MockPublisher("mockB")}
    pipeline.run(job, pubs)
    assert pubs["mockB"].publish_count == 1
    assert _disk(jobs_dir, job.id)["records"]["mockB"]["state"] == "published"


@pytest.mark.parametrize("scenario", ["server_error", "transport_timeout", "unexpected_error"])
def test_5xx_and_transport_errors_are_unknown_not_failed(approved_job, jobs_dir, scenario):
    job = approved_job("mockA")
    pipeline.run(job, {"mockA": MockPublisher("mockA", scenario)})
    rec = _disk(jobs_dir, job.id)["records"]["mockA"]
    assert rec["state"] == "unknown"
    assert rec["remote_id"] is None
    assert rec["needs_manual"] is True


def test_failed_is_retried_immediately(approved_job, jobs_dir):
    job = approved_job("mockA")
    pub = MockPublisher("mockA", ["fail", "ok"])
    pipeline.run(job, {"mockA": pub})
    assert job.records["mockA"].state == "failed"

    pipeline.retry(job, {"mockA": pub})

    assert pub.publish_count == 2
    rec = _disk(jobs_dir, job.id)["records"]["mockA"]
    assert rec["state"] == "published"
    assert rec["attempts"] == 2


def test_accept_then_timeout_reconcile_published_without_resubmit(approved_job, jobs_dir):
    job = approved_job("mockA")
    pub = MockPublisher("mockA", "accept_then_timeout", reconcile_result="published")

    pipeline.run(job, {"mockA": pub})
    rec = _disk(jobs_dir, job.id)["records"]["mockA"]
    assert rec["state"] == "unknown"
    assert rec["remote_id"] == "mockA-1"
    assert rec["needs_manual"] is False

    pipeline.retry(job, {"mockA": pub})

    assert pub.publish_count == 1
    assert pub.reconcile_calls == ["mockA-1"]
    rec = _disk(jobs_dir, job.id)["records"]["mockA"]
    assert rec["state"] == "published"
    assert rec["remote_id"] == "mockA-1"


def test_reconcile_unknown_keeps_waiting(approved_job):
    job = approved_job("mockA")
    pub = MockPublisher("mockA", "accept_then_timeout", reconcile_result="unknown")
    pipeline.run(job, {"mockA": pub})
    pipeline.retry(job, {"mockA": pub})
    rec = job.records["mockA"]
    assert rec.state == "unknown"
    assert rec.remote_id == "mockA-1"
    assert pub.publish_count == 1


def test_transport_timeout_without_remote_id_is_never_resubmitted(approved_job, jobs_dir):
    job = approved_job("mockA")
    pub = MockPublisher("mockA", ["transport_timeout", "ok"], supports_idempotency_key=True)

    pipeline.run(job, {"mockA": pub})
    rec = _disk(jobs_dir, job.id)["records"]["mockA"]
    assert rec["state"] == "unknown"
    assert rec["needs_manual"] is True

    pipeline.retry(job, {"mockA": pub})
    pipeline.retry(job, {"mockA": pub})

    assert pub.publish_count == 1
    assert pub.reconcile_calls == []
    rec = _disk(jobs_dir, job.id)["records"]["mockA"]
    assert rec["state"] == "unknown"
    assert rec["needs_manual"] is True


def test_reconcile_not_found_without_guarantee_marks_manual(approved_job, jobs_dir):
    job = approved_job("mockA")
    pub = MockPublisher("mockA", ["accept_then_timeout", "ok"], reconcile_result="not_found")

    pipeline.run(job, {"mockA": pub})
    pipeline.retry(job, {"mockA": pub})

    assert pub.publish_count == 1
    rec = _disk(jobs_dir, job.id)["records"]["mockA"]
    assert rec["state"] == "unknown"
    assert rec["needs_manual"] is True


@pytest.mark.parametrize(
    "guarantee",
    [{"supports_idempotency_key": True}, {"not_found_is_definitive": True}],
    ids=["idempotency_key", "not_found_definitive"],
)
def test_reconcile_not_found_with_guarantee_resubmits(approved_job, jobs_dir, guarantee):
    job = approved_job("mockA")
    pub = MockPublisher("mockA", ["accept_then_timeout", "ok"], reconcile_result="not_found", **guarantee)

    pipeline.run(job, {"mockA": pub})
    pipeline.retry(job, {"mockA": pub})

    assert pub.publish_count == 2
    rec = _disk(jobs_dir, job.id)["records"]["mockA"]
    assert rec["state"] == "published"
    assert rec["remote_id"] == "mockA-2"
    assert rec["url"]
    assert rec["needs_manual"] is False


def test_request_key_generated_recorded_and_reused(approved_job, jobs_dir):
    job = approved_job("mockA")
    pub = MockPublisher(
        "mockA", ["accept_then_timeout", "ok"], reconcile_result="not_found", supports_idempotency_key=True
    )

    pipeline.run(job, {"mockA": pub})
    key = _disk(jobs_dir, job.id)["records"]["mockA"]["request_key"]
    assert key
    pipeline.retry(job, {"mockA": pub})

    assert [c.request_key for c in pub.calls] == [key, key]
    assert _disk(jobs_dir, job.id)["records"]["mockA"]["request_key"] == key


def test_request_key_not_sent_when_unsupported(approved_job):
    job = approved_job("mockA")
    pub = MockPublisher("mockA")
    pipeline.run(job, {"mockA": pub})
    assert pub.calls[0].request_key is None
    assert job.records["mockA"].request_key is None


def test_request_keys_are_per_target(approved_job):
    job = approved_job("mockA", "mockB")
    pubs = {p: MockPublisher(p, supports_idempotency_key=True) for p in ("mockA", "mockB")}
    pipeline.run(job, pubs)
    keys = {p: r.request_key for p, r in job.records.items()}
    assert all(keys.values())
    assert keys["mockA"] != keys["mockB"]


def test_crash_after_send_recovers_to_unknown_without_requeue(approved_job, jobs_dir):
    job = approved_job("mockA", "mockB")
    crashing = {"mockA": MockPublisher("mockA"), "mockB": MockPublisher("mockB", "crash_after_send")}

    with pytest.raises(SimulatedCrash):
        pipeline.run(job, crashing)

    # 디스크에는 결과 저장 전 상태(publishing)가 남아 있어야 한다.
    assert _disk(jobs_dir, job.id)["records"]["mockB"]["state"] == "publishing"

    # --- 재시작: 새 프로세스처럼 디스크에서 다시 로드 ---
    reloaded = pipeline.recover(pipeline.load_job(job.id))
    rec = reloaded.records["mockB"]
    assert rec.state == "unknown"
    assert rec.needs_manual is True
    assert _disk(jobs_dir, job.id)["records"]["mockB"]["state"] == "unknown"

    fresh = {"mockA": MockPublisher("mockA"), "mockB": MockPublisher("mockB")}
    pipeline.run(reloaded, fresh)
    pipeline.retry(reloaded, fresh)

    # 블라인드 재큐 금지, published 플랫폼 재호출 금지
    assert fresh["mockA"].publish_count == 0
    assert fresh["mockB"].publish_count == 0
    assert reloaded.records["mockA"].state == "published"
    assert reloaded.records["mockB"].state == "unknown"


def test_published_platform_not_called_after_restart(approved_job, jobs_dir):
    job = approved_job("mockA", "mockB")
    pipeline.run(job, {"mockA": MockPublisher("mockA"), "mockB": MockPublisher("mockB", "fail")})

    reloaded = pipeline.recover(pipeline.load_job(job.id))
    fresh = {"mockA": MockPublisher("mockA"), "mockB": MockPublisher("mockB")}
    pipeline.run(reloaded, fresh)
    pipeline.retry(reloaded, fresh)

    assert fresh["mockA"].publish_count == 0
    assert fresh["mockA"].reconcile_calls == []
    assert fresh["mockB"].publish_count == 1
    assert _disk(jobs_dir, job.id)["records"]["mockB"]["state"] == "published"


def test_missing_publisher_does_not_block_others(approved_job):
    job = approved_job("mockA", "mockB")
    pub = MockPublisher("mockB")
    pipeline.run(job, {"mockB": pub})
    assert job.records["mockA"].state == "failed"
    assert job.records["mockB"].state == "published"


def test_atomic_save_leaves_no_temp_files(approved_job, jobs_dir):
    job = approved_job("mockA", "mockB")
    pipeline.run(job, {"mockA": MockPublisher("mockA"), "mockB": MockPublisher("mockB")})
    assert sorted(p.name for p in jobs_dir.iterdir()) == [f"{job.id}.json"]
