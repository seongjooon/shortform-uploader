from __future__ import annotations

import socket
import urllib.request

import pytest

from shortform import pipeline
from shortform.publishers import MockPublisher

from .conftest import NetworkBlocked


def test_socket_creation_is_blocked():
    with pytest.raises(NetworkBlocked):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("example.com", 443))
    with pytest.raises(Exception):
        urllib.request.urlopen("http://example.com", timeout=1)


def test_full_flow_runs_with_network_blocked(approved_job):
    job = approved_job("mockA", "mockB")
    pubs = {
        "mockA": MockPublisher("mockA", "accept_then_timeout"),
        "mockB": MockPublisher("mockB", ["fail", "ok"]),
    }
    pipeline.run(job, pubs)
    pipeline.retry(job, pubs)
    assert {p: r.state for p, r in job.records.items()} == {"mockA": "published", "mockB": "published"}
