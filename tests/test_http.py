import http.client
from unittest.mock import Mock
import pytest
from shortform.http import Transport, FileSlice, ConnectError
from shortform.publishers.base import TransportError


def test_file_slice_streams_only_requested_bytes(video):
    assert b"".join(FileSlice(video, 3, 8)) == video.read_bytes()[3:11]
    with pytest.raises(ValueError):
        len(FileSlice(video, -1, 8))


def test_connect_failure_is_definitive(monkeypatch):
    conn = Mock()
    conn.connect.side_effect = ConnectionRefusedError("secret URL")
    monkeypatch.setattr(http.client, "HTTPSConnection", lambda *a, **k: conn)
    with pytest.raises(ConnectError, match="before sending"):
        Transport().request("POST", "https://example.org", body=b"test")
    conn.request.assert_not_called()


def test_post_connect_failure_is_unknown(monkeypatch):
    conn = Mock()
    conn.getresponse.side_effect = TimeoutError("secret URL")
    monkeypatch.setattr(http.client, "HTTPSConnection", lambda *a, **k: conn)
    with pytest.raises(TransportError, match="result unknown"):
        Transport().request("POST", "https://example.org", body=b"test")
    conn.request.assert_called_once()
