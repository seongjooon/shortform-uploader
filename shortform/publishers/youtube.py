"""YouTube Data API v3 resumable-upload publisher."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Callable

from ..http import ConnectError, FileSlice, TransportError
from .base import DefinitiveRejection, PublishResult, ReconcileResult, ServerError, TimeoutAfterAccept
from .common import AuthenticatedPublisher, append_query, header, json_body, metadata, require_success, response_json


class YouTubePublisher(AuthenticatedPublisher):
    name = "youtube"
    supports_idempotency_key = False
    not_found_is_definitive = False
    upload_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
    api_url = "https://www.googleapis.com/youtube/v3/videos"

    def __init__(self, transport=None, auth=None, *, resume_attempts: int = 3) -> None:
        super().__init__(transport=transport, auth=auth)
        self.resume_attempts = resume_attempts

    @staticmethod
    def _handle(session_uri: str, size: int, account_id: str) -> str:
        raw = json.dumps({"v": 1, "u": session_uri, "s": size, "a": account_id}, separators=(",", ":")).encode()
        return "yt1:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _session(handle: str) -> tuple[str, int, str] | None:
        if not handle.startswith("yt1:"):
            return None
        try:
            encoded = handle[4:] + "=" * (-len(handle[4:]) % 4)
            value = json.loads(base64.urlsafe_b64decode(encoded))
            uri, size, account = value["u"], value["s"], value.get("a", "")
            if not isinstance(uri, str) or not isinstance(size, int) or size < 1 or not isinstance(account, str):
                return None
            return uri, size, account
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def publish(self, video_path: str, target: dict[str, Any], request_key: str | None = None,
                on_handle: Callable[[str], None] | None = None) -> PublishResult:
        path = Path(video_path)
        size = path.stat().st_size
        if size <= 0:
            raise DefinitiveRejection("YouTube video is empty")
        account_id = str(target.get("account_id") or "")
        if not account_id:
            raise DefinitiveRejection("YouTube account_id is required")
        token = self.access_token(account_id)
        info = metadata(target)
        privacy = target.get("privacy", "private")
        if privacy not in {"private", "unlisted", "public"}:
            raise DefinitiveRejection("invalid YouTube privacy")
        body = json_body({
            "snippet": {"title": str(info.get("title", "")), "description": str(info.get("description", ""))},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(info.get("made_for_kids", False))},
        })
        response = self.transport.request("POST", self.upload_url, {
            **self._headers(token), "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4",
        }, body)
        require_success(response, "YouTube resumable session", accepted=(200,))
        session_uri = header(response, "Location")
        if not session_uri:
            raise ServerError(response.status, "YouTube did not return a resumable session")
        handle = self._handle(session_uri, size, account_id)
        if on_handle:
            on_handle(handle)
        return self._upload(session_uri, size, str(path), token, handle)

    def _upload(self, session_uri: str, size: int, video_path: str, token: str, handle_value: str) -> PublishResult:
        offset = 0
        for attempt in range(self.resume_attempts + 1):
            try:
                response = self.transport.request("PUT", session_uri, {
                    **self._headers(token), "Content-Type": "video/mp4", "Content-Length": str(size - offset),
                    "Content-Range": f"bytes {offset}-{size - 1}/{size}",
                }, FileSlice(video_path, offset=offset))
            except (TransportError, ConnectError) as exc:
                # Once a session exists even a pre-byte failure of this PUT is
                # not proof of non-acceptance; query the session instead.
                if attempt >= self.resume_attempts:
                    raise TimeoutAfterAccept(handle_value, "YouTube upload interrupted") from exc
                try:
                    response = self._query_session(session_uri, size, token)
                except Exception as query_error:
                    raise TimeoutAfterAccept(handle_value, "YouTube upload recovery status is unknown") from query_error
            if response.status in (200, 201):
                data = response_json(response)
                video_id = data.get("id") if isinstance(data, dict) else None
                if not isinstance(video_id, str) or not video_id:
                    raise ServerError(response.status, "YouTube completion has no video id")
                return PublishResult(video_id, f"https://www.youtube.com/shorts/{video_id}", "posted")
            if response.status != 308:
                # A resumable PUT may have reached Google even when the final
                # response is an error.  Preserve its session handle instead
                # of converting this into a fresh video submission.
                raise TimeoutAfterAccept(handle_value, "YouTube upload response is uncertain")
            current = self._range_offset(header(response, "Range"), size)
            if current is None or current <= offset:
                if attempt >= self.resume_attempts:
                    raise TimeoutAfterAccept(handle_value, "YouTube upload progress is unknown")
                continue
            offset = current
        raise TimeoutAfterAccept(handle_value, "YouTube upload did not complete")

    def _query_session(self, uri: str, size: int, token: str):
        return self.transport.request("PUT", uri, {
            **self._headers(token), "Content-Length": "0", "Content-Range": f"bytes */{size}",
        }, b"")

    @staticmethod
    def _range_offset(value: str | None, size: int) -> int | None:
        if not value:
            return 0
        try:
            end = int(value.rsplit("-", 1)[1])
            offset = end + 1
            return offset if 0 <= offset <= size else None
        except (ValueError, IndexError):
            return None

    def reconcile(self, handle_or_id: str) -> ReconcileResult:
        session = self._session(handle_or_id)
        if session:
            uri, size, account_id = session
            # Reconciliation is read-only: a 308 tells the pipeline that a
            # resumable session exists but intentionally does not resume bytes.
            response = self._query_session(uri, size, self.access_token_for_reconcile(account_id))
            if response.status in (200, 201):
                data = response_json(response)
                video_id = data.get("id") if isinstance(data, dict) else None
                if isinstance(video_id, str) and video_id:
                    return ReconcileResult("published", video_id, f"https://www.youtube.com/shorts/{video_id}", "posted")
                return ReconcileResult("unknown", needs_manual=True)
            if response.status == 308:
                return ReconcileResult("unknown", needs_manual=True)
            if response.status == 404:
                return ReconcileResult("not_found", needs_manual=True)
            require_success(response, "YouTube session reconciliation")
        token = self.access_token_for_reconcile()
        response = self.transport.request("GET", append_query(self.api_url, part="status", id=handle_or_id), self._headers(token))
        require_success(response, "YouTube video lookup")
        data = response_json(response)
        items = data.get("items", []) if isinstance(data, dict) else []
        if items:
            return ReconcileResult("published", handle_or_id, f"https://www.youtube.com/shorts/{handle_or_id}", "posted")
        return ReconcileResult("not_found", needs_manual=True)

    def access_token_for_reconcile(self, account_id: str | None = None) -> str:
        # The persisted handle intentionally omits account data. AuthManager can
        # still use its one configured YouTube binding for a status read.
        if self._auth is None:
            from ..auth import AuthManager
            self._auth = AuthManager(self.name, transport=self.transport)
        return self._auth.access_token(account_id=account_id)
