from shortform import service
from shortform.models import PlatformTarget
from shortform.publishers.mock import MockPublisher


def test_plan_and_upload_share_approved_metadata(video, tmp_path):
    target = PlatformTarget("youtube", "account", "private", {"made_for_kids": False, "title": "old"})
    plan = service.plan(video, "Hello", ["#one"], ["youtube"], "", {"youtube": target})
    assert plan.targets["youtube"].metadata == {
        "made_for_kids": False, "title": "Hello", "description": "Hello\n\n#one"
    }
    events = []
    job = service.upload(plan, tmp_path, publishers_factory=lambda targets: {"youtube": MockPublisher("youtube")},
                         on_done=lambda platform, record: events.append((platform, record.state)))
    assert events == [("youtube", "published")]
    assert service.history(tmp_path)[0].id == job.id


def test_submit_step_rejects_invalid_value_without_marking(tmp_path):
    from shortform.auth import TokenStore
    from shortform.setup import Progress
    store = TokenStore(tmp_path)
    step = next(s for s in service.setup_steps("youtube") if s.is_field)
    assert service.submit_step("youtube", step.id, "invalid", store=store)
    assert step.id not in Progress(store).done_steps("youtube")
