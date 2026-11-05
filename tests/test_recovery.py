import pytest
from shortform import pipeline
from shortform.publishers.base import ReconcileResult, PublishResult, TransportError
from shortform.publishers.mock import MockPublisher, SimulatedCrash


class HandlePublisher(MockPublisher):
    def publish(self, video_path, target, request_key=None, on_handle=None):
        on_handle("session-123")
        raise SimulatedCrash()


def test_handle_survives_crash_and_reconciles(approved_job):
    job = approved_job("mockA")
    with pytest.raises(SimulatedCrash):
        pipeline.run(job, {"mockA": HandlePublisher("mockA")})
    restored = pipeline.recover(pipeline.load_job(job.id))
    assert restored.records["mockA"].handle == "session-123"
    assert not restored.records["mockA"].needs_manual
    pub = MockPublisher("mockA", reconcile_result=ReconcileResult("published", "video-123", "https://example.org/video", "posted"))
    pipeline.retry(restored, {"mockA": pub})
    assert pub.reconcile_calls == ["session-123"]
    assert pub.publish_count == 0
    assert restored.records["mockA"].remote_id == "video-123"
    assert restored.records["mockA"].delivery == "posted"


def test_rejected_waits_until_next_retry(approved_job):
    job = approved_job("mockA")
    pub = MockPublisher("mockA", "accept_then_timeout", reconcile_result=ReconcileResult("rejected"))
    pipeline.run(job, {"mockA": pub})
    pipeline.retry(job, {"mockA": pub})
    assert job.records["mockA"].state == "failed"
    assert pub.publish_count == 1
    pub.scenarios = ["ok"]
    pipeline.retry(job, {"mockA": pub})
    assert pub.publish_count == 2


def test_manual_rejection_never_resubmits(approved_job):
    job = approved_job("mockA")
    pub = MockPublisher("mockA", "accept_then_timeout", reconcile_result=ReconcileResult("rejected", needs_manual=True))
    pipeline.run(job, {"mockA": pub})
    pipeline.retry(job, {"mockA": pub})
    pipeline.retry(job, {"mockA": pub})
    assert pub.publish_count == 1
    assert job.records["mockA"].needs_manual


def test_handle_transport_error_is_reconcilable(approved_job):
    class Pub(MockPublisher):
        def publish(self, *args, on_handle=None, **kwargs):
            on_handle("h")
            raise TransportError("disconnected")
    job = approved_job("mockA")
    pipeline.run(job, {"mockA": Pub("mockA")})
    assert job.records["mockA"].state == "unknown"
    assert not job.records["mockA"].needs_manual


def test_status_observes_inbox_without_resubmitting(approved_job):
    job = approved_job("mockA")
    rec = job.records["mockA"]
    rec.state = "published"
    rec.delivery = "inbox"
    rec.handle = "publish-id"
    pub = MockPublisher("mockA", reconcile_result=ReconcileResult("published", delivery="posted"))
    pipeline.refresh_status(job, {"mockA": pub})
    assert rec.delivery == "posted"
    assert pub.publish_count == 0


def test_job_lock_prevents_concurrent_processing(approved_job):
    job = approved_job("mockA")
    with pipeline.job_lock(job.id):
        with pytest.raises(RuntimeError, match="다른 프로세스"):
            with pipeline.job_lock(job.id):
                pass


def test_job_id_cannot_escape_jobs_dir():
    with pytest.raises(ValueError):
        pipeline.job_path("../../escape")
