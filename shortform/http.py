"""The sole network boundary. Redirects are never followed with credentials or bodies."""
from __future__ import annotations

import http.client
import json
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .publishers.base import DefinitiveRejection, TransportError


class ConnectError(DefinitiveRejection):
    """Connection failed before any HTTP request bytes were sent."""


@dataclass
class FileSlice:
    path: str | Path
    offset: int = 0
    length: int | None = None

    def __len__(self):
        size = Path(self.path).stat().st_size
        length = size - self.offset if self.length is None else self.length
        if self.offset < 0 or length < 0 or self.offset + length > size:
            raise ValueError("Invalid upload file slice")
        return length

    def __iter__(self):
        remaining = len(self)
        with open(self.path, "rb") as stream:
            stream.seek(self.offset)
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError("Upload file changed while reading")
                remaining -= len(chunk)
                yield chunk


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes = b""

    def json(self):
        return json.loads(self.body)


class Transport:
    def request(self, method, url, headers=None, body=None, timeout=60):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise DefinitiveRejection("API URL must use HTTPS without credentials")
        connection = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=timeout)
        try:
            # Explicit connect is the only stage known to precede HTTP transmission.
            try:
                connection.connect()
            except (OSError, ssl.SSLError, socket.timeout):
                raise ConnectError("API connection failed before sending request") from None
            request_headers = dict(headers or {})
            if body is not None:
                request_headers.setdefault("Content-Length", str(len(body)))
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            try:
                connection.request(method, path, body=body, headers=request_headers)
                response = connection.getresponse()
                return Response(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
            except (OSError, http.client.HTTPException):
                raise TransportError("API transmission interrupted; result unknown") from None
        finally:
            connection.close()


class FakeTransport:
    """Deterministic, socket-free transport for adapter contract tests."""
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, body=None, timeout=60):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "body": body, "timeout": timeout})
        if not self.responses:
            raise AssertionError("No fake response queued")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result
