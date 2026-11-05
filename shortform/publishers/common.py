"""Small provider-neutral helpers for the live REST publishers."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from ..http import Response, Transport
from .base import DefinitiveRejection, ServerError


def metadata(target: dict[str, Any]) -> dict[str, Any]:
    value = target.get("metadata") or {}
    if not isinstance(value, dict):
        raise DefinitiveRejection("platform metadata must be an object")
    return value


def json_body(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def form_body(value: dict[str, Any]) -> bytes:
    return urlencode({k: _form_value(v) for k, v in value.items() if v is not None}).encode("utf-8")


def _form_value(value: Any) -> str:
    return "true" if value is True else "false" if value is False else str(value)


def append_query(url: str, **params: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"


def response_json(response: Response) -> Any:
    try:
        return response.json()
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ServerError(response.status, "API returned invalid JSON") from exc


def header(response: Response, name: str) -> str | None:
    for key, value in response.headers.items():
        if key.lower() == name.lower():
            return value
    return None


def require_success(response: Response, action: str, *, accepted: tuple[int, ...] | None = None) -> Response:
    if (response.status in accepted) if accepted is not None else (200 <= response.status < 300):
        return response
    if 400 <= response.status < 500:
        # Do not include untrusted response text: upstream proxies sometimes
        # echo credential-bearing request data in their diagnostics.
        raise DefinitiveRejection(f"{action} rejected (HTTP {response.status})")
    raise ServerError(response.status, f"{action} outcome is uncertain (HTTP {response.status})")


class AuthenticatedPublisher:
    """Lazy token binding shared by all live publishers."""

    name: str

    def __init__(self, *, transport: Transport | None = None, auth: Any | None = None) -> None:
        self.transport = transport or Transport()
        self._auth = auth

    def access_token(self, account_id: str) -> str:
        if self._auth is None:
            from ..auth import AuthManager

            self._auth = AuthManager(self.name, transport=self.transport)
        token = self._auth.access_token(account_id=account_id)
        if not isinstance(token, str) or not token:
            raise DefinitiveRejection(f"{self.name} token is unavailable for this account")
        return token
