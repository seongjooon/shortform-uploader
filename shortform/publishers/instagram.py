"""Instagram Reels publisher using Meta's resumable-upload container flow."""

from __future__ import annotations

from pathlib import Path
from time import sleep as default_sleep
from typing import Any, Callable

from ..http import ConnectError, FileSlice, TransportError
from .base import DefinitiveRejection, PublishResult, ReconcileResult, ServerError, TimeoutAfterAccept
from .common import AuthenticatedPublisher, append_query, form_body, metadata, require_success, response_json


class InstagramPublisher(AuthenticatedPublisher):
    name = "instagram"
    supports_idempotency_key = False
    not_found_is_definitive = False

    def __init__(self, transport=None, auth=None, sleep=default_sleep, *, api_version: str | None = None,
                 poll_interval: int = 60, poll_attempts: int = 5) -> None:
        super().__init__(transport=transport, auth=auth)
        self.sleep = sleep
        self.api_version = api_version or "v24.0"
        self._explicit_version = api_version is not None
        self.poll_interval = poll_interval
        self.poll_attempts = poll_attempts

    def _graph(self, suffix: str) -> str:
        if not self._explicit_version and self._auth is not None:
            credentials = getattr(self._auth, "credentials", {})
            if isinstance(credentials, dict):
                self.api_version = credentials.get("graph_version", "v24.0")
        return f"https://graph.facebook.com/{self.api_version}/{suffix.lstrip('/')}"

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _check_limit(self, account_id: str, token: str) -> None:
        response = self.transport.request("GET", append_query(self._graph(f"{account_id}/content_publishing_limit"), fields="quota_usage,config"), self._headers(token))
        require_success(response, "Instagram publishing-limit lookup")
        data = response_json(response)
        entries = data.get("data", []) if isinstance(data, dict) else []
        entry = entries[0] if isinstance(entries, list) and entries else None
        config = entry.get("config") if isinstance(entry, dict) else None
        used = entry.get("quota_usage") if isinstance(entry, dict) else None
        total = config.get("quota_total") if isinstance(config, dict) else None
        # Meta's prose has stated conflicting fixed quotas.  Require the
        # account-specific response requested above instead of guessing.
        if not isinstance(used, int) or not isinstance(total, int) or total < 1:
            raise ServerError(response.status, "Instagram publishing limit could not be verified")
        if used >= total:
            raise DefinitiveRejection("Instagram content publishing limit reached")

    def publish(self, video_path: str, target: dict[str, Any], request_key: str | None = None,
                on_handle: Callable[[str], None] | None = None) -> PublishResult:
        account_id = str(target.get("account_id") or "")
        if not account_id:
            raise DefinitiveRejection("Instagram account_id is required")
        size = Path(video_path).stat().st_size
        if size <= 0:
            raise DefinitiveRejection("Instagram video is empty")
        token = self.access_token(account_id)
        self._check_limit(account_id, token)
        info = metadata(target)
        create = self.transport.request("POST", self._graph(f"{account_id}/media"), {
            **self._headers(token), "Content-Type": "application/x-www-form-urlencoded"
        }, form_body({"media_type": "REELS", "upload_type": "resumable", "caption": info.get("caption", ""),
                      "share_to_feed": info.get("share_to_feed", True)}))
        require_success(create, "Instagram Reel container", accepted=(200,))
        result = response_json(create)
        container_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(container_id, str) or not container_id:
            raise TimeoutAfterAccept(None, "Instagram returned no container id")
        if on_handle:
            on_handle(container_id)
        try:
            uploaded = self.transport.request("POST", f"https://rupload.facebook.com/ig-api-upload/{self.api_version}/{container_id}", {
                "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
                "Content-Type": "application/octet-stream",
            }, FileSlice(video_path))
        except (ConnectError, TransportError) as exc:
            raise TimeoutAfterAccept(container_id, "Instagram Reel upload interrupted") from exc
        require_success(uploaded, "Instagram Reel upload")
        try:
            state = self._wait_for_finished(container_id, token)
        except Exception as exc:
            # A container exists already, so a failed status lookup is not a
            # safe basis for a new submission.
            raise TimeoutAfterAccept(container_id, "Instagram processing status is unknown") from exc
        if state == "ERROR" or state == "EXPIRED":
            raise DefinitiveRejection(f"Instagram container {state.lower()}")
        if state != "FINISHED":
            raise TimeoutAfterAccept(container_id, "Instagram container is still processing")
        try:
            published = self.transport.request("POST", self._graph(f"{account_id}/media_publish"), {
                **self._headers(token), "Content-Type": "application/x-www-form-urlencoded"
            }, form_body({"creation_id": container_id}))
        except (ConnectError, TransportError) as exc:
            raise TimeoutAfterAccept(container_id, "Instagram publish interrupted") from exc
        require_success(published, "Instagram media publish", accepted=(200,))
        data = response_json(published)
        media_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(media_id, str) or not media_id:
            raise TimeoutAfterAccept(container_id, "Instagram publish returned no media id")
        try:
            permalink = self._permalink(media_id, token)
        except Exception:
            # The publish endpoint returned a durable media id.  Permalink is
            # presentation data only; a lookup failure cannot turn success
            # into a retryable failure.
            permalink = None
        return PublishResult(media_id, permalink, "posted")

    def _status(self, container_id: str, token: str) -> str:
        response = self.transport.request("GET", append_query(self._graph(container_id), fields="status_code"), self._headers(token))
        require_success(response, "Instagram container status")
        data = response_json(response)
        value = data.get("status_code") if isinstance(data, dict) else None
        return value if isinstance(value, str) else "IN_PROGRESS"

    def _wait_for_finished(self, container_id: str, token: str) -> str:
        for number in range(self.poll_attempts):
            status = self._status(container_id, token)
            if status in {"FINISHED", "ERROR", "EXPIRED", "PUBLISHED"}:
                return status
            if number + 1 < self.poll_attempts:
                self.sleep(self.poll_interval)
        return "IN_PROGRESS"

    def _permalink(self, media_id: str, token: str) -> str | None:
        response = self.transport.request("GET", append_query(self._graph(media_id), fields="permalink"), self._headers(token))
        require_success(response, "Instagram permalink lookup")
        data = response_json(response)
        value = data.get("permalink") if isinstance(data, dict) else None
        return value if isinstance(value, str) else None

    def reconcile(self, handle_or_id: str) -> ReconcileResult:
        # A container ID has no embedded account identifier. AuthManager checks
        # its configured account binding before a status read.
        if self._auth is None:
            from ..auth import AuthManager
            self._auth = AuthManager(self.name, transport=self.transport)
        token = self._auth.access_token()
        status = self._status(handle_or_id, token)
        if status == "PUBLISHED":
            return ReconcileResult("published", handle_or_id, delivery="posted")
        if status == "FINISHED":
            return ReconcileResult("not_found", needs_manual=True)
        if status in {"ERROR", "EXPIRED"}:
            return ReconcileResult("rejected")
        return ReconcileResult("unknown")
