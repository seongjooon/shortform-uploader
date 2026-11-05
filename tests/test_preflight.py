from __future__ import annotations

import json
import subprocess

from shortform.models import PlatformTarget
from shortform.preflight import check_video


def _targets(*platforms: str):
    return {name: PlatformTarget(name, "account", "private") for name in platforms}


def _probe(monkeypatch, *, duration="30", width=1080, height=1920, fps="30/1"):
    payload = {"format": {"duration": duration}, "streams": [{"codec_type": "video", "width": width, "height": height, "r_frame_rate": fps}]}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, json.dumps(payload), ""),
    )


def test_check_video_reports_platform_specific_probe_errors(video, monkeypatch):
    _probe(monkeypatch, duration="181", width=1920, height=1080, fps="70/1")

    result = check_video(video, _targets("youtube", "instagram", "tiktok"))

    assert len(result.errors["youtube"]) == 2
    assert len(result.errors["tiktok"]) == 1
    assert result.errors["instagram"] == []
    assert result.ok is False


def test_check_video_missing_ffprobe_is_a_warning(video, monkeypatch):
    def absent(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", absent)
    result = check_video(video, _targets("youtube", "tiktok"))
    assert result.ok is True
    assert result.errors == {"youtube": [], "tiktok": []}
    assert "ffprobe" in result.warnings[0]


def test_check_video_rejects_nonempty_mp4_requirement(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.touch()
    result = check_video(empty, _targets("youtube", "tiktok"))
    assert all("비어" in messages[0] for messages in result.errors.values())
