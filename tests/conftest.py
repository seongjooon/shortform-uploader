from __future__ import annotations

import socket
import os

import pytest

from shortform import pipeline
from shortform.models import PlatformTarget


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("mock 단계에서는 네트워크 사용 금지")


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """모든 테스트에서 소켓 생성/연결/이름 해석을 전역 차단한다."""
    if request.node.get_closest_marker("live") and os.environ.get("SHORTFORM_LIVE") == "1":
        return
    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket, "socketpair", _blocked)


@pytest.fixture(autouse=True)
def jobs_dir(tmp_path, monkeypatch):
    d = tmp_path / "jobs"
    monkeypatch.setenv("SHORTFORM_JOBS_DIR", str(d))
    return d


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4)
    return path


def make_targets(*platforms: str) -> dict[str, PlatformTarget]:
    return {
        p: PlatformTarget(
            provider="mock",
            account_id="me",
            privacy="private",
            metadata={"title": "T", "caption": f"caption-{p}", "description": "D"},
        )
        for p in platforms
    }


@pytest.fixture
def approved_job(video):
    def factory(*platforms: str):
        job = pipeline.create_job(video, make_targets(*platforms))
        pipeline.approve(job)
        return job

    return factory


def pytest_collection_modifyitems(config, items):
    if os.environ.get("SHORTFORM_LIVE") != "1":
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(pytest.mark.skip(reason="SHORTFORM_LIVE=1 required"))
