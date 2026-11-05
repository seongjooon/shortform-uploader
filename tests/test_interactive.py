from __future__ import annotations

from pathlib import Path

import pytest

from shortform import interactive, pipeline, preflight
from shortform.models import FAILED, PUBLISHED, PlatformRecord
from shortform.publishers.mock import FAIL, MockPublisher


class FakeStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def load(self, platform):
        return self.tokens.get(platform)


@pytest.fixture(autouse=True)
def no_ffprobe(monkeypatch):
    skip = lambda _path: (None, "ffprobe 생략")
    monkeypatch.setattr(interactive, "_probe", skip)
    monkeypatch.setattr(preflight, "_probe", skip)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("/Users/me/My\\ Clip\\ (1).mp4 ", "/Users/me/My Clip (1).mp4"),
        ("'/Users/me/My Clip.mp4'", "/Users/me/My Clip.mp4"),
        ('"/Users/me/My Clip.mp4"', "/Users/me/My Clip.mp4"),
        ("file:///Users/me/My%20Clip.mp4", "/Users/me/My Clip.mp4"),
    ],
)
def test_clean_dropped_path(raw, expected):
    assert interactive.clean_dropped_path(raw) == Path(expected)


def test_clean_dropped_path_expands_home():
    assert interactive.clean_dropped_path("~/a.mp4") == Path.home() / "a.mp4"


def test_parse_hashtags_normalizes_and_dedupes():
    assert interactive.parse_hashtags(" 러닝, #run  ##Run shorts,,") == ["#러닝", "#run", "#shorts"]


def test_compose_caption_and_default_title(tmp_path):
    assert interactive.compose_caption("  오늘의 러닝\n5km ", ["#run"]) == "오늘의 러닝\n5km\n\n#run"
    assert interactive.compose_caption("", []) == ""
    assert interactive.default_title("\n오늘의 러닝\n5km", tmp_path / "x.mp4") == "오늘의 러닝"
    assert interactive.default_title("", tmp_path / "clip.mp4") == "clip"
    assert len(interactive.default_title("a" * 300, tmp_path / "x.mp4")) == 100


def test_available_targets_from_tokens_only():
    store = FakeStore({"youtube": {"account_id": "UC1"}, "tiktok": {"account_id": "open1"}})
    targets, unavailable = interactive.available_targets(None, store)
    assert list(targets) == ["youtube", "tiktok"]
    assert targets["youtube"].privacy == "private"
    assert targets["tiktok"].privacy == "inbox"
    assert "instagram" in unavailable


def test_available_targets_rejects_account_mismatch(tmp_path):
    config = tmp_path / "shortform.toml"
    config.write_text(
        '[platforms.youtube]\nenabled = true\naccount_id = "UC1"\n'
        '[platforms.instagram]\nenabled = true\naccount_id = "IG1"\n'
        '[platforms.tiktok]\nenabled = false\n'
    )
    store = FakeStore({"youtube": {"account_id": "UC1"}, "instagram": {"account_id": "OTHER"}})
    targets, unavailable = interactive.available_targets(config, store)
    assert list(targets) == ["youtube"]
    assert "account_id" in unavailable["instagram"]
    assert unavailable["tiktok"] == "설정에서 꺼져 있음"


def test_select_platforms_accepts_numbers_names_and_default():
    names = ["youtube", "instagram", "tiktok"]
    out = []
    answers = iter(["9", "3,1,3"])
    assert interactive.select_platforms(names, lambda _p: next(answers), out.append) == ["tiktok", "youtube"]
    assert any("목록에 있는 번호" in line for line in out)
    assert interactive.select_platforms(names, lambda _p: "", out.append) == names


def test_done_message_explains_tiktok_inbox():
    rec = PlatformRecord("tiktok", PUBLISHED, delivery="inbox")
    title, message = interactive.done_message("tiktok", rec, "job1")
    assert "전송 완료" in title and "게시" in message
    failed = PlatformRecord("youtube", FAILED, error="quota")
    assert interactive.done_message("youtube", failed, "job1") == ("YouTube Shorts 업로드 실패", "quota")


def _wizard(video, answers, *, tokens=None, scenarios=None):
    if tokens is None:
        tokens = {"youtube": {"account_id": "UC1"}, "instagram": {"account_id": "IG1"}}
    store = FakeStore(tokens)
    publishers = {}

    def factory(targets):
        for p in targets:
            publishers[p] = MockPublisher(p, (scenarios or {}).get(p, "ok"))
        return publishers

    notes, out = [], []
    feed = iter(answers)
    code = interactive.run_wizard(
        publishers_factory=factory, store=store, input_func=lambda _p: next(feed),
        out=out.append, notifier=lambda t, m: notes.append((t, m)),
    )
    return code, publishers, notes, out


def test_wizard_uploads_with_caption_hashtags_and_notifies(video):
    dropped = str(video).replace(" ", "\\ ")
    code, publishers, notes, _out = _wizard(video, [dropped, "오늘의 러닝", "run, #러닝", "", "", "y"])

    assert code == 0
    yt = publishers["youtube"].calls[0].target["metadata"]
    assert yt == {"title": "오늘의 러닝", "description": "오늘의 러닝\n\n#run #러닝"}
    assert publishers["instagram"].calls[0].target["metadata"] == {"caption": "오늘의 러닝\n\n#run #러닝"}
    assert [t for t, _m in notes] == ["YouTube Shorts 업로드 완료", "Instagram Reels 업로드 완료"]


def test_wizard_reprompts_for_bad_path_and_respects_selection(video, tmp_path):
    answers = [str(tmp_path / "missing.mp4"), str(video), "cap", "", "2", "y"]
    code, publishers, notes, out = _wizard(video, answers)

    assert code == 0
    assert list(publishers) == ["instagram"]
    assert any("찾을 수 없어요" in line for line in out)
    assert len(notes) == 1


def test_wizard_declined_approval_creates_no_job(video, jobs_dir):
    code, publishers, notes, _out = _wizard(video, [str(video), "cap", "", "", "", "n"])

    assert code == 0
    assert publishers == {} and notes == []
    assert not jobs_dir.exists()


def test_wizard_reports_failure_per_platform(video):
    code, _publishers, notes, _out = _wizard(
        video, [str(video), "cap", "", "", "", "y"], scenarios={"instagram": FAIL}
    )

    assert code == 1
    assert [t for t, _m in notes] == ["YouTube Shorts 업로드 완료", "Instagram Reels 업로드 실패"]


def test_wizard_without_accounts_exits_early(video):
    code, _publishers, _notes, out = _wizard(video, [], tokens={})

    assert code == 2
    assert any("shortform setup" in line for line in out)


def test_pipeline_run_calls_on_done_for_each_platform(approved_job):
    job = approved_job("mockA", "mockB")
    seen = []
    pipeline.run(job, {"mockA": MockPublisher("mockA")}, on_done=lambda p, r: seen.append((p, r.state)))
    assert seen == [("mockA", PUBLISHED), ("mockB", FAILED)]


def test_wizard_accepts_video_given_on_command_line(video):
    store = FakeStore({"youtube": {"account_id": "UC1"}})
    pubs = {}
    feed = iter(["cap", "", "", "y"])  # no video prompt: caption, hashtags, title, approve
    code = interactive.run_wizard(
        publishers_factory=lambda t: pubs.update({p: MockPublisher(p) for p in t}) or pubs,
        store=store, input_func=lambda _p: next(feed), out=lambda _l: None,
        notifier=lambda t, m: None, video=str(video),
    )
    assert code == 0 and pubs["youtube"].calls


def test_wizard_asks_again_when_given_video_is_missing(video, tmp_path):
    store = FakeStore({"youtube": {"account_id": "UC1"}})
    out, feed = [], iter([str(video), "cap", "", "", "n"])
    interactive.run_wizard(publishers_factory=lambda t: {}, store=store, input_func=lambda _p: next(feed),
                           out=out.append, notifier=lambda t, m: None, video=str(tmp_path / "nope.mp4"))
    assert any("찾을 수 없어요" in line for line in out)


def test_cli_treats_bare_path_as_upload(monkeypatch, video):
    from shortform import cli
    seen = {}
    monkeypatch.setattr(cli, "_cmd_upload", lambda args: seen.setdefault("video", args.video) and 0)
    cli.main([str(video)])
    assert seen["video"] == str(video)


def test_wizard_copies_tiktok_caption_after_inbox_upload(video):
    store = FakeStore({"tiktok": {"account_id": "open1"}})
    copied, out = [], []
    feed = iter(["오늘 러닝", "run", "y"])
    class InboxPublisher:
        def publish(self, *_args, **_kwargs):
            from shortform.publishers.base import PublishResult
            return PublishResult("pid", delivery="inbox")

    code = interactive.run_wizard(
        publishers_factory=lambda t: {p: InboxPublisher() for p in t}, store=store,
        input_func=lambda _p: next(feed), out=out.append, notifier=lambda t, m: None,
        video=str(video), copy=lambda v: copied.append(v) or True,
    )
    assert code == 0
    assert copied == ["오늘 러닝\n\n#run"]
    assert any("붙여 넣어" in line for line in out)



def test_wizard_does_not_copy_caption_for_direct_post(video):
    from shortform.models import PlatformRecord
    rec = PlatformRecord("tiktok", PUBLISHED, url="https://www.tiktok.com/@_/video/1", delivery="posted",
                         note="'나만 보기'로 올렸어요.")
    title, message = interactive.done_message("tiktok", rec, "job")
    assert title == "TikTok 업로드 완료" and "나만 보기" in message
