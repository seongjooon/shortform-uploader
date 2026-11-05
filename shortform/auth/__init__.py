"""OAuth credential handling for the local shortform CLI.

Credentials live outside the repository in ``$SHORTFORM_CONFIG_DIR`` (or
``~/.config/shortform``).  Create ``credentials.json`` with only the platform
entries you use::

    {
      "youtube": {"client_id": "...", "client_secret": "..."},
      "instagram": {"client_id": "...", "client_secret": "...",
                    "config_id": "...", "redirect_uri": "https://...",
                    "account_id": "1784...", "graph_version": "v24.0"},
      "tiktok": {"client_key": "...", "client_secret": "...",
                 "redirect_uri": "https://example.com/tiktok/callback"}
    }

``client_id`` is required for YouTube (``client_secret`` is accepted when a
desktop-client JSON provides it).  Instagram requires ``client_id``,
``client_secret``, Facebook Login for Business ``config_id``, and
``redirect_uri``; ``account_id`` is optional but required when discovery finds
more than one linked professional account.  TikTok requires every shown key.

Each successful authorization is stored separately as
``tokens/<platform>.json`` with mode 0600.  Token files contain access and
refresh tokens, expiry, and the verified platform account binding; they must
never be committed or printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import webbrowser
from getpass import getpass
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

from shortform.http import Response, Transport
from shortform.publishers.base import DefinitiveRejection


YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
TIKTOK_SCOPES = ("user.info.basic", "video.upload", "video.publish")
META_SCOPES = (
    "instagram_basic",
    "instagram_content_publish",
    "pages_read_engagement",
    "pages_show_list",
    "business_management",
)


class AuthRequired(DefinitiveRejection):
    """Credentials are absent, expired, rejected, or bound to another account."""


def config_dir() -> Path:
    """Return the private application config directory without creating it."""
    configured = os.environ.get("SHORTFORM_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".config" / "shortform"


class TokenStore:
    """Atomic, private token and credential storage outside the working tree."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory).expanduser() if directory else config_dir()

    def _token_path(self, platform: str) -> Path:
        if platform not in {"youtube", "instagram", "tiktok"}:
            raise ValueError("unsupported platform")
        return self.directory / "tokens" / f"{platform}.json"

    @property
    def credentials_path(self) -> Path:
        return self.directory / "credentials.json"

    def load(self, platform: str) -> dict[str, Any] | None:
        path = self._token_path(platform)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthRequired("stored authentication data is invalid; authenticate again") from exc
        if not isinstance(data, dict):
            raise AuthRequired("stored authentication data is invalid; authenticate again")
        return data

    def save(self, platform: str, token: Mapping[str, Any]) -> None:
        path = self._token_path(platform)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        payload = json.dumps(dict(token), ensure_ascii=False, sort_keys=True).encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{platform}-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def credentials(self, platform: str) -> dict[str, Any]:
        try:
            all_credentials = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            value = all_credentials[platform]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AuthRequired(f"{platform} credentials are missing") from exc
        if not isinstance(value, dict):
            raise AuthRequired(f"{platform} credentials are invalid")
        return dict(value)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def validate_callback(params: Mapping[str, str], expected_state: str) -> str:
    """Validate a decoded OAuth callback before handing its code to a provider."""
    if not secrets.compare_digest(params.get("state", ""), expected_state):
        raise AuthRequired("OAuth callback state did not match")
    if params.get("error"):
        raise AuthRequired("authorization was declined or rejected")
    code = params.get("code")
    if not code:
        raise AuthRequired("OAuth callback did not include an authorization code")
    return code


def callback_handler(expected_state: str, sink: dict[str, str], expected_path: str = "/oauth/callback") -> type[BaseHTTPRequestHandler]:
    """Make the small loopback callback handler; exposed for socket-free tests."""
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - HTTP API spelling
            if urlparse(self.path).path != expected_path:
                self.send_response(404)
                self.end_headers()
                return
            params = {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items()}
            try:
                sink["code"] = validate_callback(params, expected_state)
            except AuthRequired as exc:
                sink["error"] = str(exc)
                self.send_response(400)
                message = b"Authorization rejected. You may close this window."
            else:
                self.send_response(200)
                message = b"Authorization completed. You may close this window."
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

        def log_message(self, format: str, *args: object) -> None:
            return

    return CallbackHandler


def loopback_authorize(
    auth_url_builder: Callable[[str, str, str], str],
    token_exchanger: Callable[[str, str], Mapping[str, Any]],
    *,
    browser_open: Callable[[str], Any] = webbrowser.open,
    server_factory: Callable[..., HTTPServer] = HTTPServer,
) -> dict[str, Any]:
    """Run a single local OAuth callback with state validation and PKCE S256."""
    state = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    server = server_factory(("127.0.0.1", 0), callback_handler(state, sink := {}))
    try:
        host, port = server.server_address[:2]
        redirect_uri = f"http://{host}:{port}/oauth/callback"
        browser_open(auth_url_builder(redirect_uri, state, challenge))
        deadline = time.monotonic() + 180
        while not sink and time.monotonic() < deadline:
            server.timeout = min(1.0, deadline - time.monotonic())
            server.handle_request()
    finally:
        server.server_close()
    if "error" in sink:
        raise AuthRequired(sink["error"])
    if "code" not in sink:
        raise AuthRequired("OAuth callback was not received")
    return dict(token_exchanger(sink["code"], verifier))


def _form(values: Mapping[str, Any]) -> bytes:
    return urlencode({key: value for key, value in values.items() if value is not None}).encode("utf-8")


def _json(response: Response, context: str) -> dict[str, Any]:
    if not 200 <= response.status < 300:
        raise AuthRequired(f"{context} was rejected; authenticate again")
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthRequired(f"{context} returned an invalid response") from exc
    if not isinstance(value, dict):
        raise AuthRequired(f"{context} returned an invalid response")
    return value


def _expiry(data: Mapping[str, Any]) -> float | None:
    raw = data.get("expires_at")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class AuthManager:
    """Provider OAuth flows and refreshes, using the one injectable HTTP boundary."""

    platform: str
    transport: Transport | Any | None = None
    store: TokenStore | None = None
    browser_open: Callable[[str], Any] = webbrowser.open
    input_func: Callable[[str], str] = getpass
    now: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if self.platform not in {"youtube", "instagram", "tiktok"}:
            raise ValueError("unsupported platform")
        self.transport = self.transport or Transport()
        self.store = self.store or TokenStore()

    @property
    def credentials(self) -> dict[str, Any]:
        return self.store.credentials(self.platform)

    def access_token(self, account_id: str | None = None) -> str:
        token = self.store.load(self.platform)
        if not token:
            raise AuthRequired(f"{self.platform} is not authenticated")
        if account_id is not None and token.get("account_id") != account_id:
            raise AuthRequired(f"{self.platform} token is bound to a different account")
        expires_at = _expiry(token)
        if expires_at is not None and expires_at <= self.now() + 60:
            token = self.refresh_if_needed(token)
            if account_id is not None and token.get("account_id") != account_id:
                raise AuthRequired(f"{self.platform} token is bound to a different account")
        value = token.get("access_token")
        if not isinstance(value, str) or not value:
            raise AuthRequired(f"{self.platform} authentication is incomplete")
        return value

    def refresh_if_needed(self, token: dict[str, Any] | None = None) -> dict[str, Any]:
        token = dict(token or self.store.load(self.platform) or {})
        if not token:
            raise AuthRequired(f"{self.platform} is not authenticated")
        expiry = _expiry(token)
        if expiry is None or expiry > self.now() + 60:
            return token
        try:
            if self.platform == "youtube":
                fresh = self._youtube_refresh(token)
            elif self.platform == "tiktok":
                fresh = self._tiktok_refresh(token)
            else:
                # A Page token may not carry an expiry but remains revocable.
                # Reauthorization is safer than treating a user-token exchange
                # as a general-purpose refresh operation.
                raise AuthRequired("Instagram token expired; authenticate again")
        except AuthRequired:
            raise
        except Exception as exc:
            raise AuthRequired(f"{self.platform} token refresh failed; authenticate again") from exc
        self.store.save(self.platform, fresh)
        return fresh

    def authorize(self) -> dict[str, Any]:
        if self.platform == "youtube":
            token = self._youtube_authorize()
        elif self.platform == "instagram":
            token = self._instagram_authorize()
        else:
            token = self._tiktok_authorize()
        self.store.save(self.platform, token)
        return token

    authenticate = authorize

    def _request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None, body: bytes | None = None) -> Response:
        try:
            return self.transport.request(method, url, headers=dict(headers or {}), body=body, timeout=60)
        except DefinitiveRejection:
            raise
        except Exception as exc:
            raise AuthRequired("authentication service could not be reached") from exc

    def _youtube_authorize(self) -> dict[str, Any]:
        credentials = self.credentials
        client_id = _required(credentials, "client_id")

        def build(redirect: str, state: str, challenge: str) -> str:
            return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
                "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
                "scope": " ".join(YOUTUBE_SCOPES), "access_type": "offline", "prompt": "consent",
                "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            })

        # Keep the generic callback loop deliberately reusable while retaining redirect URI in builder.
        redirect_holder: dict[str, str] = {}
        def build_with_redirect(redirect: str, state: str, challenge: str) -> str:
            redirect_holder["uri"] = redirect
            return build(redirect, state, challenge)
        def exchange_with_redirect(code: str, verifier: str) -> dict[str, Any]:
            data = _json(self._request("POST", "https://oauth2.googleapis.com/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=_form({"code": code, "client_id": client_id, "client_secret": credentials.get("client_secret"), "redirect_uri": redirect_holder["uri"], "grant_type": "authorization_code", "code_verifier": verifier})), "YouTube authorization")
            return self._youtube_bound(data)
        return loopback_authorize(build_with_redirect, exchange_with_redirect, browser_open=self.browser_open)

    def _youtube_bound(self, data: Mapping[str, Any]) -> dict[str, Any]:
        access = _required(data, "access_token")
        identity = _json(self._request("GET", "https://www.googleapis.com/youtube/v3/channels?" + urlencode({"part": "id", "mine": "true"}), headers={"Authorization": f"Bearer {access}"}), "YouTube account lookup")
        items = identity.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise AuthRequired("YouTube channel could not be verified")
        account_id = items[0].get("id")
        if not isinstance(account_id, str) or not account_id:
            raise AuthRequired("YouTube channel could not be verified")
        return _token(data, account_id=account_id)

    def _youtube_refresh(self, token: Mapping[str, Any]) -> dict[str, Any]:
        refresh = _required(token, "refresh_token")
        c = self.credentials
        data = _json(self._request("POST", "https://oauth2.googleapis.com/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=_form({"client_id": _required(c, "client_id"), "client_secret": c.get("client_secret"), "refresh_token": refresh, "grant_type": "refresh_token"})), "YouTube token refresh")
        updated = _token(data, account_id=_required(token, "account_id"), previous=token)
        return updated

    def _tiktok_authorize(self) -> dict[str, Any]:
        c = self.credentials
        state = secrets.token_urlsafe(32)
        redirect = _required(c, "redirect_uri")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = "https://www.tiktok.com/v2/auth/authorize?" + urlencode({"client_key": _required(c, "client_key"), "response_type": "code", "scope": ",".join(TIKTOK_SCOPES), "redirect_uri": redirect, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        self.browser_open(url)
        callback = self.input_func("Paste the complete TikTok redirect URL: ")
        _validate_redirect(callback, redirect)
        parsed = urlparse(callback)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        code = validate_callback(params, state)
        data = _json(self._request("POST", "https://open.tiktokapis.com/v2/oauth/token/", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=_form({"client_key": _required(c, "client_key"), "client_secret": _required(c, "client_secret"), "code": code, "grant_type": "authorization_code", "redirect_uri": redirect, "code_verifier": verifier})), "TikTok authorization")
        return self._tiktok_bound(data)

    def _tiktok_bound(self, data: Mapping[str, Any]) -> dict[str, Any]:
        account = data.get("open_id")
        if not isinstance(account, str) or not account:
            raise AuthRequired("TikTok account could not be verified")
        return _token(data, account_id=account)

    def _tiktok_refresh(self, token: Mapping[str, Any]) -> dict[str, Any]:
        c = self.credentials
        data = _json(self._request("POST", "https://open.tiktokapis.com/v2/oauth/token/", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=_form({"client_key": _required(c, "client_key"), "client_secret": _required(c, "client_secret"), "refresh_token": _required(token, "refresh_token"), "grant_type": "refresh_token"})), "TikTok token refresh")
        account_id = _required(token, "account_id")
        if data.get("open_id") != account_id:
            raise AuthRequired("TikTok token refresh could not verify the account")
        return _token(data, account_id=account_id, previous=token)

    def _instagram_authorize(self) -> dict[str, Any]:
        c = self.credentials
        version = c.get("graph_version", "v24.0")
        state = secrets.token_urlsafe(32)
        redirect = _required(c, "redirect_uri")
        url = f"https://www.facebook.com/{version}/dialog/oauth?" + urlencode({"client_id": _required(c, "client_id"), "config_id": _required(c, "config_id"), "redirect_uri": redirect, "state": state, "response_type": "code", "scope": ",".join(META_SCOPES)})
        self.browser_open(url)
        callback = self.input_func("Paste the complete Facebook redirect URL: ")
        _validate_redirect(callback, redirect)
        params = {key: values[-1] for key, values in parse_qs(urlparse(callback).query).items()}
        code = validate_callback(params, state)
        short = _json(self._request("POST", f"https://graph.facebook.com/{version}/oauth/access_token", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=_form({"client_id": _required(c, "client_id"), "client_secret": _required(c, "client_secret"), "redirect_uri": redirect, "code": code})), "Facebook authorization")
        long = _json(self._request("GET", f"https://graph.facebook.com/{version}/oauth/access_token?" + urlencode({"grant_type": "fb_exchange_token", "client_id": _required(c, "client_id"), "client_secret": _required(c, "client_secret"), "fb_exchange_token": _required(short, "access_token")})), "Facebook long-lived token exchange")
        return self._instagram_bound(long)

    def _instagram_bound(self, user_token: Mapping[str, Any]) -> dict[str, Any]:
        c = self.credentials
        version = c.get("graph_version", "v24.0")
        user_access = _required(user_token, "access_token")
        pages = _json(self._request("GET", f"https://graph.facebook.com/{version}/me/accounts?fields=id,access_token,instagram_business_account", headers={"Authorization": f"Bearer {user_access}"}), "Facebook page discovery")
        choices = pages.get("data")
        if not isinstance(choices, list):
            raise AuthRequired("Instagram business account could not be discovered")
        # Pages owned by a Business Portfolio may not be returned by /me/accounts.
        # With business_management, query portfolio-owned pages as a fallback.
        if not any(isinstance(p, dict) and isinstance(p.get("instagram_business_account"), dict) for p in choices):
            try:
                businesses = _json(self._request("GET", f"https://graph.facebook.com/{version}/me/businesses?fields=id", headers={"Authorization": f"Bearer {user_access}"}), "Facebook business discovery").get("data", [])
                for business in businesses if isinstance(businesses, list) else []:
                    if not isinstance(business, dict) or not isinstance(business.get("id"), str):
                        continue
                    owned = _json(self._request("GET", f"https://graph.facebook.com/{version}/{business['id']}/owned_pages?fields=id,access_token,instagram_business_account", headers={"Authorization": f"Bearer {user_access}"}), "Facebook business page discovery").get("data", [])
                    if isinstance(owned, list):
                        choices.extend(owned)
            except AuthRequired:
                pass
        matches: list[tuple[str, str, Any]] = []
        requested_account = c.get("account_id")
        for page in choices:
            if not isinstance(page, dict) or not isinstance(page.get("instagram_business_account"), dict):
                continue
            ig_id = page["instagram_business_account"].get("id")
            page_token = page.get("access_token")
            if isinstance(ig_id, str) and isinstance(page_token, str) and ig_id and page_token:
                if requested_account is None or requested_account == ig_id:
                    matches.append((ig_id, page_token, page.get("id")))
        if len(matches) == 1:
            ig_id, page_token, page_id = matches[0]
            result = _token(user_token, account_id=ig_id)
            result.update({"access_token": page_token, "page_id": page_id, "user_access_token": user_access})
            return result
        if len(matches) > 1:
            raise AuthRequired("multiple Instagram accounts found; set credentials.instagram.account_id")
        raise AuthRequired("no Instagram professional account is linked to an accessible Facebook Page")


def _required(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise AuthRequired("authentication response is incomplete")
    return value


def _validate_redirect(callback_url: str, registered_redirect: str) -> None:
    callback = urlparse(callback_url)
    registered = urlparse(registered_redirect)
    if (callback.scheme, callback.netloc, callback.path) != (registered.scheme, registered.netloc, registered.path):
        raise AuthRequired("OAuth callback did not use the registered redirect URI")


def _token(data: Mapping[str, Any], *, account_id: str, previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = dict(previous or {})
    result.update({key: value for key, value in data.items() if key in {"access_token", "refresh_token", "scope", "refresh_expires_in"}})
    result["access_token"] = _required(data, "access_token")
    result["account_id"] = account_id
    expires = data.get("expires_in")
    if isinstance(expires, (int, float)):
        result["expires_at"] = time.time() + float(expires)
    return result


def authenticate(platform: str, **kwargs: Any) -> dict[str, Any]:
    """CLI-friendly single-platform authorization entry point."""
    return AuthManager(platform, **kwargs).authorize()
