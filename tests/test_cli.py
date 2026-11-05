from __future__ import annotations

import pytest

from shortform import cli, pipeline
from shortform.publishers import mock


@pytest.fixture
def forbid_side_effects(monkeypatch):
    calls = []

    def record(name):
        def _fn(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"dry-run에서 {name} 호출됨")

        return _fn

    monkeypatch.setattr(mock.MockPublisher, "publish", record("publish"))
    monkeypatch.setattr(mock.MockPublisher, "reconcile", record("reconcile"))
    monkeypatch.setattr(pipeline, "approve", record("approve"))
    monkeypatch.setattr(pipeline, "run", record("run"))
    monkeypatch.setattr(pipeline, "create_job", record("create_job"))
    return calls


def test_dry_run_never_calls_publisher(video, jobs_dir, capsys, forbid_side_effects):
    code = cli.main(["run", str(video), "--title", "T", "--platforms", "mockA,mockB", "--dry-run"])

    assert code == 0
    assert forbid_side_effects == []
    assert not jobs_dir.exists()
    out = capsys.readouterr().out
    assert "mockA" in out and "mockB" in out
    assert "dry-run" in out


def test_dry_run_missing_video_fails(tmp_path, capsys, forbid_side_effects):
    code = cli.main(["run", str(tmp_path / "x.mp4"), "--title", "T", "--platforms", "mockA", "--dry-run"])
    assert code == 2
    assert forbid_side_effects == []


def test_dry_run_rejects_live_platform(video, forbid_side_effects):
    code = cli.main(["run", str(video), "--title", "T", "--platforms", "youtube", "--dry-run"])
    assert code == 2


def test_run_requires_interactive_approval(video, jobs_dir, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code = cli.main(["run", str(video), "--title", "T", "--platforms", "mockA"])
    assert code == 0
    assert not jobs_dir.exists()


def test_run_with_approval_publishes(video, jobs_dir, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    code = cli.main(["run", str(video), "--title", "T", "--platforms", "mockA,mockB"])
    assert code == 0
    assert len(list(jobs_dir.glob("*.json"))) == 1
    out = capsys.readouterr().out
    assert out.count("published") == 2


def test_status_of_existing_mock_job_never_publishes(approved_job, monkeypatch, capsys):
    job = approved_job("mockA")
    pipeline.run(job, {"mockA": mock.MockPublisher("mockA")})
    monkeypatch.setattr(mock.MockPublisher, "publish", lambda *a, **k: pytest.fail("status submitted content"))
    assert cli.main(["status", job.id]) == 0
    assert "published" in capsys.readouterr().out


def test_retry_failed_mock_job(approved_job):
    job = approved_job("mockA")
    pipeline.run(job, {"mockA": mock.MockPublisher("mockA", "fail")})
    assert cli.main(["retry", job.id]) == 0
    assert pipeline.load_job(job.id).records["mockA"].state == "published"


def test_config_dry_run_ignores_empty_error_lists(video, tmp_path, monkeypatch, forbid_side_effects):
    from shortform.preflight import PreflightResult
    import shortform.preflight
    monkeypatch.setattr(shortform.preflight, "check_video", lambda *a: PreflightResult({"youtube": []}, []))
    config = tmp_path / "shortform.toml"
    config.write_text('[defaults]\ntitle="T"\n[platforms.youtube]\nenabled=true\naccount_id="UCtest"\n')
    assert cli.main(["run", str(video), "--config", str(config), "--dry-run"]) == 0
    assert forbid_side_effects == []


@pytest.mark.parametrize("option", ["--caption", "--description", "--account", "--privacy"])
def test_config_rejects_silently_ignored_mock_options(video, option):
    assert cli.main(["run", str(video), "--config", "unused.toml", option, "value", "--dry-run"]) == 2


def test_interactive_platform_selection_single_platform(video, tmp_path, monkeypatch, jobs_dir, capsys):
    from shortform.preflight import PreflightResult
    import shortform.preflight
    monkeypatch.setattr(shortform.preflight, "check_video", lambda *a: PreflightResult())
    from shortform.publishers.mock import MockPublisher
    monkeypatch.setattr(cli, "_publishers", lambda targets: {p: MockPublisher(p) for p in targets})

    config = tmp_path / "shortform.toml"
    config.write_text("""[defaults]
title = "T"
caption = "C"
[platforms.youtube]
enabled = true
account_id = "UC1"
[platforms.tiktok]
enabled = true
account_id = "TK1"
""")
    # First input is platform selection "2" (tiktok), second is approval "y"
    inputs = iter(["2", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    code = cli.main(["run", str(video), "--config", str(config), "--jobs-dir", str(jobs_dir)])
    assert code == 0
    job = pipeline.load_job(next(jobs_dir.glob("*.json")).stem, jobs_dir)
    assert set(job.records.keys()) == {"tiktok"}
    assert job.records["tiktok"].state == "published"


def test_interactive_platform_selection_multiple_platforms(video, tmp_path, monkeypatch, jobs_dir):
    from shortform.preflight import PreflightResult
    import shortform.preflight
    monkeypatch.setattr(shortform.preflight, "check_video", lambda *a: PreflightResult())
    from shortform.publishers.mock import MockPublisher
    monkeypatch.setattr(cli, "_publishers", lambda targets: {p: MockPublisher(p) for p in targets})

    config = tmp_path / "shortform.toml"
    config.write_text("""[defaults]
title = "T"
caption = "C"
[platforms.youtube]
enabled = true
account_id = "UC1"
[platforms.instagram]
enabled = true
account_id = "IG1"
[platforms.tiktok]
enabled = true
account_id = "TK1"
""")
    # Select platforms 1 and 3 (youtube, tiktok)
    inputs = iter(["1, 3", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    code = cli.main(["run", str(video), "--config", str(config), "--jobs-dir", str(jobs_dir)])
    assert code == 0
    job = pipeline.load_job(next(jobs_dir.glob("*.json")).stem, jobs_dir)
    assert set(job.records.keys()) == {"youtube", "tiktok"}


def test_interactive_platform_selection_by_name(video, tmp_path, monkeypatch, jobs_dir):
    from shortform.preflight import PreflightResult
    import shortform.preflight
    monkeypatch.setattr(shortform.preflight, "check_video", lambda *a: PreflightResult())
    from shortform.publishers.mock import MockPublisher
    monkeypatch.setattr(cli, "_publishers", lambda targets: {p: MockPublisher(p) for p in targets})

    config = tmp_path / "shortform.toml"
    config.write_text("""[defaults]
title = "T"
caption = "C"
[platforms.youtube]
enabled = true
account_id = "UC1"
[platforms.tiktok]
enabled = true
account_id = "TK1"
""")
    # Select by name "tiktok"
    inputs = iter(["tiktok", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    code = cli.main(["run", str(video), "--config", str(config), "--jobs-dir", str(jobs_dir)])
    assert code == 0
    job = pipeline.load_job(next(jobs_dir.glob("*.json")).stem, jobs_dir)
    assert set(job.records.keys()) == {"tiktok"}


def test_interactive_platform_selection_default_all_on_enter(video, tmp_path, monkeypatch, jobs_dir):
    from shortform.preflight import PreflightResult
    import shortform.preflight
    monkeypatch.setattr(shortform.preflight, "check_video", lambda *a: PreflightResult())
    from shortform.publishers.mock import MockPublisher
    monkeypatch.setattr(cli, "_publishers", lambda targets: {p: MockPublisher(p) for p in targets})

    config = tmp_path / "shortform.toml"
    config.write_text("""[defaults]
title = "T"
caption = "C"
[platforms.youtube]
enabled = true
account_id = "UC1"
[platforms.tiktok]
enabled = true
account_id = "TK1"
""")
    # Hit Enter ("") for default all, then "y" for approval
    inputs = iter(["", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    code = cli.main(["run", str(video), "--config", str(config), "--jobs-dir", str(jobs_dir)])
    assert code == 0
    job = pipeline.load_job(next(jobs_dir.glob("*.json")).stem, jobs_dir)
    assert set(job.records.keys()) == {"youtube", "tiktok"}

