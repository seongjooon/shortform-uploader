from __future__ import annotations

import json
import stat

import pytest

from shortform import setup
from shortform.auth import AuthRequired, TokenStore


def feed(*answers, prompts=None):
    it = iter(answers)

    def ask(prompt):
        if prompts is not None:
            prompts.append(prompt)
        return next(it)

    return ask


class Recorder:
    def __init__(self):
        self.out, self.opened, self.copied = [], [], []

    def kwargs(self):
        return {"out": self.out.append, "open_url": self.opened.append,
                "copy": lambda v: self.copied.append(v) or True}

    def text(self):
        return "\n".join(self.out)


def fake_auth(results):
    """Each authorize() call pops one result: a pasted URL (callback flow), an exception, or 'ok'."""
    calls = []

    class FakeAuth:
        def __init__(self, platform, *, store, input_func, browser_open):
            self.platform, self.store, self.input_func = platform, store, input_func

        def authorize(self):
            calls.append(self.platform)
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            if result == "paste":
                self.input_func("Paste the URL: ")
            self.store.save(self.platform, {"access_token": "t", "account_id": "acct-1"})

    return FakeAuth, calls


# ------------------------------------------------------------- validators

def test_youtube_client_accepts_dropped_json(tmp_path):
    path = tmp_path / "client secret.json"
    path.write_text(json.dumps({"installed": {"client_id": "x.apps.googleusercontent.com", "client_secret": "GOCSPX-abc"}}))
    updates, error = setup._youtube_client(str(path).replace(" ", "\\ "), {})
    assert error is None
    assert updates == {"client_id": "x.apps.googleusercontent.com", "client_secret": "GOCSPX-abc"}


def test_youtube_client_rejects_wrong_values(tmp_path):
    bad = tmp_path / "other.json"
    bad.write_text("{}")
    assert setup._youtube_client(str(bad), {})[1]
    assert setup._youtube_client("GOCSPX-oops", {})[1]
    assert setup._youtube_client("1.apps.googleusercontent.com", {}) == ({"client_id": "1.apps.googleusercontent.com"}, None)


def test_simple_validators():
    assert setup._digits("client_id", "앱 ID", range(10, 21))("1000000000000001", {})[0] == {"client_id": "1000000000000001"}
    assert setup._digits("client_id", "앱 ID", range(10, 21))("abc", {})[1]
    assert setup._hex_secret("client_secret", "시크릿")("0123456789abcdef0123456789abcdef", {})[1] is None
    assert setup._hex_secret("client_secret", "시크릿")("••••", {})[1]
    assert setup._tiktok_secret("samekey12345", {"client_key": "samekey12345"})[1]
    assert setup._tiktok_redirect("", {})[0] == {"redirect_uri": setup.TIKTOK_DEFAULT_REDIRECT}
    assert "https" in setup._tiktok_redirect("http://localhost/", {})[1]


# ------------------------------------------------------------- diagnosis

def test_diagnose_callback_tiktok_errors():
    url = "https://www.tiktok.com/v2/auth/authorize?client_key=x&error=unauthorized_client&error_type=client_key"
    assert "Sandbox" in " ".join(setup.diagnose_callback("tiktok", url, "https://e.com/cb").steps)
    url = "https://www.tiktok.com/v2/auth/authorize?error=invalid_request&error_type=redirect_uri"
    problem = setup.diagnose_callback("tiktok", url, "https://e.com/cb")
    assert "Redirect URI" in problem.title and "https://e.com/cb" in problem.steps[0]
    assert "취소" in setup.diagnose_callback("instagram", "http://localhost/?error=access_denied", "").title
    assert setup.diagnose_callback("tiktok", "https://e.com/cb?code=abc&state=s", "https://e.com/cb") is None


def test_diagnose_instagram_page_link():
    problem = setup.diagnose("instagram", "no Instagram professional account is linked to an accessible Facebook Page", {})
    assert problem.url and "linked_instagram" in problem.url
    assert any("Switch Page" in s for s in problem.steps)


# ------------------------------------------------------------- progress and coach

def test_progress_merges_and_keeps_file_private(tmp_path):
    progress = setup.Progress(TokenStore(tmp_path))
    progress.save_creds("tiktok", {"client_key": "k"})
    progress.save_creds("tiktok", {"client_secret": "s"})
    progress.save_creds("youtube", {"client_id": "y"})
    assert progress.creds("tiktok") == {"client_key": "k", "client_secret": "s"}
    assert stat.S_IMODE((tmp_path / "credentials.json").stat().st_mode) == 0o600
    progress.mark("tiktok", "app")
    assert progress.done_steps("tiktok") == ["app"]
    progress.mark("tiktok", None)
    assert progress.done_steps("tiktok") == []


def test_coach_ask_helps_after_repeated_failures():
    rec = Recorder()
    coach = setup.Coach(feed("?", "x", "y", "z", "1234567890"), rec.out.append, rec.opened.append, lambda v: True)
    result = coach.ask("ID:", setup._digits("config_id", "구성 ID", range(10, 21)), {}, ["도움말 내용"], "https://u")
    assert result == {"config_id": "1234567890"}
    assert rec.text().count("도움말 내용") == 2  # explicit '?' and automatic after 3 failures
    assert "천천히" in rec.text()


def test_coach_quit_raises_pause():
    coach = setup.Coach(feed("q"), print, lambda u: None, lambda v: True)
    with pytest.raises(setup.SetupPaused):
        coach.wait([], None)


# ------------------------------------------------------------- full flows

TIKTOK_ANSWERS = ["", "", "", "", "sbawkey123456", "SecretValue1234567890"]


def test_tiktok_flow_diagnoses_rekeys_and_succeeds(tmp_path):
    store, rec = TokenStore(tmp_path), Recorder()
    auth, calls = fake_auth(["paste", "paste"])
    answers = TIKTOK_ANSWERS + [
        "https://www.tiktok.com/v2/auth/authorize?error=unauthorized_client&error_type=client_key",
        "r", "", "", "sbawkey654321", "OtherSecret1234567890",
        "not a url", "https://example.com/tiktok/callback?code=abc&state=s",
    ]
    code = setup.run_setup(["tiktok"], store=store, input_func=feed(*answers), auth_factory=auth, **rec.kwargs())

    assert code == 0
    assert calls == ["tiktok", "tiktok"]
    assert store.load("tiktok")["account_id"] == "acct-1"
    creds = json.loads((tmp_path / "credentials.json").read_text())["tiktok"]
    assert creds == {"redirect_uri": setup.TIKTOK_DEFAULT_REDIRECT, "client_key": "sbawkey654321",
                     "client_secret": "OtherSecret1234567890"}
    assert setup.TIKTOK_DEFAULT_REDIRECT in rec.copied
    assert "Target users" in rec.text() and "연결 완료" in rec.text()
    assert "http로 시작하는" in rec.text()


def test_retry_after_auth_error_without_rekey(tmp_path):
    store, rec = TokenStore(tmp_path), Recorder()
    auth, calls = fake_auth([AuthRequired("no Instagram professional account is linked to an accessible Facebook Page"), "ok"])
    answers = ["", "", "", "", "2000000000000002", "1000000000000001", "0123456789abcdef0123456789abcdef", "o", ""]
    code = setup.run_setup(["instagram"], store=store, input_func=feed(*answers), auth_factory=auth, **rec.kwargs())

    assert code == 0 and calls == ["instagram", "instagram"]
    assert "https://www.facebook.com/settings/?tab=linked_instagram" in rec.opened
    creds = json.loads((tmp_path / "credentials.json").read_text())["instagram"]
    assert creds["redirect_uri"] == "http://localhost/" and creds["config_id"] == "2000000000000002"


def test_pause_and_resume(tmp_path):
    store, rec = TokenStore(tmp_path), Recorder()
    auth, calls = fake_auth(["paste"])
    assert setup.run_setup(["tiktok"], store=store, input_func=feed("", "", "q"), auth_factory=auth, **rec.kwargs()) == 0
    assert "이어서" in rec.text() and calls == []
    assert setup.Progress(store).done_steps("tiktok") == ["app", "sandbox"]

    rec2, prompts = Recorder(), []
    answers = ["", "", "", "sbawkey123456", "SecretValue1234567890", "https://example.com/tiktok/callback?code=a&state=s"]
    assert setup.run_setup(["tiktok"], store=store, input_func=feed(*answers, prompts=prompts), auth_factory=auth, **rec2.kwargs()) == 0
    assert "2/6단계" in prompts[0]
    assert "[TikTok 3/6]" in rec2.text() and "[TikTok 1/6]" not in rec2.text()
    assert store.load("tiktok") and setup.Progress(store).done_steps("tiktok") == []


def test_youtube_json_skips_secret_step(tmp_path):
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "x.apps.googleusercontent.com", "client_secret": "GOCSPX-abcdefghijklmnop"}}))
    store, rec = TokenStore(tmp_path / "cfg"), Recorder()
    auth, calls = fake_auth(["ok"])
    code = setup.run_setup(["youtube"], store=store, input_func=feed("", "", "", str(client)), auth_factory=auth, **rec.kwargs())
    assert code == 0 and calls == ["youtube"]
    assert "클라이언트 보안 비밀번호" not in rec.text().split("[YouTube Shorts 4/5]")[1]


def test_already_connected_is_skipped(tmp_path):
    store, rec = TokenStore(tmp_path), Recorder()
    store.save("youtube", {"access_token": "t", "account_id": "UC1"})
    auth, calls = fake_auth([])
    assert setup.run_setup(["youtube"], store=store, input_func=feed("n"), auth_factory=auth, **rec.kwargs()) == 0
    assert calls == []


def test_guide_explains_why_success_and_cheers(tmp_path):
    rec = Recorder()
    auth, _calls = fake_auth([])
    answers = ["", "", "http://insecure", "q"]
    setup.run_setup(["tiktok"], store=TokenStore(tmp_path), input_func=feed(*answers), auth_factory=auth, **rec.kwargs())
    text = rec.text()
    assert "약 10분" in text and "준비물" in text          # intro: time and what to prepare
    assert "왜 하나요?" in text and "✅" in text           # every step says why and what success looks like
    assert "한 단계 끝" in text                           # progress encouragement
    assert "앗, TikTok은 https://" in text                # gentle, specific correction
    assert "여기까지 저장해 뒀어요" in text


def test_existing_app_values_jump_straight_to_connect(tmp_path):
    store, rec = TokenStore(tmp_path), Recorder()
    setup.Progress(store).save_creds("tiktok", {"redirect_uri": setup.TIKTOK_DEFAULT_REDIRECT,
                                                "client_key": "sbawkey123456", "client_secret": "SecretValue1234567890"})
    auth, calls = fake_auth(["paste"])
    answers = ["", "https://example.com/tiktok/callback?code=a&state=s"]
    assert setup.run_setup(["tiktok"], store=store, input_func=feed(*answers), auth_factory=auth, **rec.kwargs()) == 0
    assert calls == ["tiktok"]
    assert "[TikTok 1/6]" not in rec.text() and "연결 완료" in rec.text()
