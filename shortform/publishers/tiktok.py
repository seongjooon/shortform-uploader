"""TikTok Content Posting API: inbox (draft) upload or Direct Post with caption."""

from __future__ import annotations

import re
from pathlib import Path
from time import sleep as default_sleep
from typing import Any, Callable

from ..http import ConnectError, FileSlice, TransportError
from .base import DefinitiveRejection, PublishResult, ReconcileResult, ServerError, TimeoutAfterAccept
from .common import AuthenticatedPublisher, json_body, require_success, response_json


MiB = 1024 * 1024

# Config privacy name -> TikTok privacy_level, ordered from most to least public.
PRIVACY_LEVELS = {
    "public": "PUBLIC_TO_EVERYONE",
    "followers": "FOLLOWER_OF_CREATOR",
    "friends": "MUTUAL_FOLLOW_FRIENDS",
    "private": "SELF_ONLY",
}
_KOREAN_LEVEL = {"PUBLIC_TO_EVERYONE": "전체 공개", "FOLLOWER_OF_CREATOR": "팔로워 공개",
                 "MUTUAL_FOLLOW_FRIENDS": "친구 공개", "SELF_ONLY": "나만 보기"}
_CAPTION_MAX = 2200
_DEFINITIVE_INIT_ERRORS = frozenset({
    "scope_not_authorized", "spam_risk_too_many_pending_share", "spam_risk_too_many_posts",
    "spam_risk_user_banned_from_posting", "rate_limit_exceeded", "access_token_invalid", "invalid_param",
    "privacy_level_option_mismatch", "unaudited_client_can_only_post_to_private_accounts",
    "reached_active_user_cap",
})


class TikTokPublisher(AuthenticatedPublisher):
    name = "tiktok"
    supports_idempotency_key = False
    not_found_is_definitive = False
    init_url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
    direct_init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    creator_info_url = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
    status_url = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

    def __init__(self, transport=None, auth=None, sleep=default_sleep, *, poll_interval: int = 2,
                 poll_attempts: int = 5, direct_poll_attempts: int = 30) -> None:
        super().__init__(transport=transport, auth=auth)
        self.sleep = sleep
        self.poll_interval = poll_interval
        self.poll_attempts = poll_attempts
        # A direct post is transcoded and published server-side, which takes longer than an inbox hand-off.
        self.direct_poll_attempts = direct_poll_attempts

    @staticmethod
    def chunk_plan(size: int) -> list[tuple[int, int]]:
        """Return (offset, length) chunks compliant with TikTok's current rules.

        Chunks are 10 MiB where possible.  A trailing fragment below 5 MiB is
        merged into its predecessor, as required by the media-transfer guide.
        """
        if size <= 0:
            raise DefinitiveRejection("TikTok video is empty")
        standard = 10 * MiB
        if size < standard:
            return [(0, size)]
        # TikTok specifies floor(video_size / chunk_size) for the declared
        # count.  The final part therefore absorbs *all* remainder, not merely
        # a sub-5 MiB tail (26 MiB is 10 MiB + 16 MiB, two chunks).
        count = size // standard
        return [(index * standard, standard) for index in range(count - 1)] + [
            ((count - 1) * standard, size - (count - 1) * standard)
        ]

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _payload(response, *, init: bool = False) -> dict[str, Any]:
        value = response_json(response)
        if not isinstance(value, dict):
            raise ServerError(response.status, "TikTok returned malformed response")
        error = value.get("error") or {}
        if isinstance(error, dict) and error.get("code") not in (None, "ok"):
            code = str(error.get("code"))
            # Only these documented init responses prove that the server did
            # not accept the new inbox upload.  A status response comes after
            # acceptance and must remain uncertain on every API error.
            if init and code in _DEFINITIVE_INIT_ERRORS:
                raise DefinitiveRejection(_explain_init_error(code))
            raise ServerError(response.status, "TikTok API returned an uncertain error")
        data = value.get("data") or {}
        if not isinstance(data, dict):
            raise ServerError(response.status, "TikTok returned malformed data")
        return data

    def publish(self, video_path: str, target: dict[str, Any], request_key: str | None = None,
                on_handle: Callable[[str], None] | None = None) -> PublishResult:
        account_id = str(target.get("account_id") or "")
        if not account_id:
            raise DefinitiveRejection("TikTok account_id is required")
        size = Path(video_path).stat().st_size
        plan = self.chunk_plan(size)
        token = self.access_token(account_id)
        privacy = str(target.get("privacy") or "inbox")
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": plan[0][1], "total_chunk_count": len(plan)}
        note = None
        if privacy == "inbox":
            url, body, label = self.init_url, {"source_info": source}, "TikTok inbox upload initialization"
        else:
            if privacy not in PRIVACY_LEVELS:
                raise DefinitiveRejection(f"unknown TikTok privacy: {privacy}")
            level, note = self._choose_level(PRIVACY_LEVELS[privacy], token)
            caption = str((target.get("metadata") or {}).get("caption", ""))[:_CAPTION_MAX]
            url, label = self.direct_init_url, "TikTok direct post initialization"
            body = {"post_info": {"title": caption, "privacy_level": level}, "source_info": source}
        init = self.transport.request("POST", url, {
            **self._headers(token), "Content-Type": "application/json; charset=UTF-8"
        }, json_body(body))
        if 400 <= init.status < 500:
            code = _error_code(init)
            if code:
                raise DefinitiveRejection(_explain_init_error(code))
        require_success(init, label, accepted=(200,))
        data = self._payload(init, init=True)
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not isinstance(publish_id, str) or not publish_id or not isinstance(upload_url, str) or not upload_url:
            raise TimeoutAfterAccept(None, "TikTok returned no upload handle")
        if on_handle:
            on_handle(publish_id)
        for offset, length in plan:
            try:
                uploaded = self.transport.request("PUT", upload_url, {
                    "Content-Type": "video/mp4", "Content-Length": str(length),
                    "Content-Range": f"bytes {offset}-{offset + length - 1}/{size}",
                }, FileSlice(video_path, offset, length))
            except (ConnectError, TransportError) as exc:
                raise TimeoutAfterAccept(publish_id, "TikTok chunk upload interrupted") from exc
            # TikTok returns 206 for non-final chunks and 201 for completion.
            if uploaded.status not in (200, 201, 206):
                raise TimeoutAfterAccept(publish_id, "TikTok chunk result requires remote confirmation")
        attempts = self.poll_attempts if privacy == "inbox" else self.direct_poll_attempts
        status, status_data = self._wait_for_inbox(publish_id, token, attempts)
        if status == "SEND_TO_USER_INBOX":
            return PublishResult(publish_id, delivery="inbox")
        if status == "PUBLISH_COMPLETE":
            post_id = self._post_id(status_data)
            url = f"https://www.tiktok.com/@_/video/{post_id}" if post_id else None
            return PublishResult(post_id or publish_id, url, "posted", note)
        if status == "FAILED":
            # Keep the saved publish id so retry can fetch the failure reason
            # and decide whether a human needs to intervene.
            raise TimeoutAfterAccept(publish_id, "TikTok reported processing failure")
        raise TimeoutAfterAccept(publish_id, "TikTok is still processing upload")

    def _fetch(self, publish_id: str, token: str) -> tuple[str, dict[str, Any]]:
        try:
            response = self.transport.request("POST", self.status_url, {
                **self._headers(token), "Content-Type": "application/json; charset=UTF-8"
            }, json_body({"publish_id": publish_id}))
            require_success(response, "TikTok upload status")
            data = self._payload(response)
        except Exception:
            # The upload already has a publish id.  A failed status read cannot
            # establish that it failed, and must never trigger a blind resend.
            return "PROCESSING_UPLOAD", {}
        status = data.get("status")
        return (status if isinstance(status, str) else "PROCESSING_UPLOAD"), data

    def _wait_for_inbox(self, publish_id: str, token: str, attempts: int | None = None) -> tuple[str, dict[str, Any]]:
        attempts = attempts or self.poll_attempts
        for number in range(attempts):
            status, data = self._fetch(publish_id, token)
            if status in {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE", "FAILED"}:
                return status, data
            if number + 1 < attempts:
                self.sleep(self.poll_interval)
        return "PROCESSING_UPLOAD", {}

    def _choose_level(self, wanted: str, token: str) -> tuple[str, str | None]:
        """Use the requested privacy, or the nearest more-private level TikTok allows for this creator."""
        response = self.transport.request("POST", self.creator_info_url, {
            **self._headers(token), "Content-Type": "application/json; charset=UTF-8"
        }, b"{}")
        require_success(response, "TikTok creator info", accepted=(200,))
        options = self._payload(response, init=True).get("privacy_level_options") or []
        order = list(PRIVACY_LEVELS.values())
        for level in order[order.index(wanted):]:
            if level in options:
                if level == wanted:
                    return level, None
                return level, (f"TikTok이 이 계정에 '{_KOREAN_LEVEL[wanted]}'를 허용하지 않아 "
                               f"'{_KOREAN_LEVEL[level]}'로 올렸어요. 심사 전 앱은 보통 '나만 보기'만 가능해요.")
        raise DefinitiveRejection(f"TikTok이 허용한 공개 범위가 없어요 (허용: {', '.join(map(str, options)) or '없음'})")

    @staticmethod
    def _post_id(data: dict[str, Any]) -> str | None:
        # The API documents this misspelled field as list<int64>: an inbox
        # draft can become more than one creator post, so retain the first
        # publicly available id in this single-result model.
        value = data.get("publicaly_available_post_id")
        if isinstance(value, list) and value:
            value = value[0]
        if isinstance(value, (str, int)) and str(value):
            return str(value)
        return None

    def reconcile(self, handle_or_id: str) -> ReconcileResult:
        if self._auth is None:
            from ..auth import AuthManager
            self._auth = AuthManager(self.name, transport=self.transport)
        token = self._auth.access_token()
        status, data = self._fetch(handle_or_id, token)
        if status == "SEND_TO_USER_INBOX":
            return ReconcileResult("published", handle_or_id, delivery="inbox")
        if status == "PUBLISH_COMPLETE":
            post_id = self._post_id(data)
            url = f"https://www.tiktok.com/@_/video/{post_id}" if post_id else None
            return ReconcileResult("published", post_id or handle_or_id, url, "posted")
        if status == "FAILED":
            reason = str(data.get("fail_reason", ""))
            return ReconcileResult("rejected", needs_manual=reason.startswith(("auth_removed", "spam_risk")))
        return ReconcileResult("unknown")


def _error_code(response) -> str | None:
    """Only TikTok's short machine-readable error code is surfaced, never free response text."""
    try:
        error = response_json(response).get("error") or {}
        code = error.get("code") if isinstance(error, dict) else None
    except Exception:
        return None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else None


def _explain_init_error(code: str) -> str:
    messages = {
        "unaudited_client_can_only_post_to_private_accounts":
            "심사 전 TikTok 앱은 비공개 계정에만 바로 게시할 수 있어요. TikTok 앱에서 계정을 비공개로 바꾸거나, "
            "설정의 TikTok privacy를 'inbox'로 바꿔 받은편지함으로 보내세요.",
        "privacy_level_option_mismatch": "TikTok이 허용하지 않는 공개 범위예요. TikTok privacy를 'private'로 바꿔 보세요.",
        "scope_not_authorized": "TikTok 권한이 부족해요. `shortform-uploader setup tiktok`으로 다시 연결해 주세요.",
        "access_token_invalid": "TikTok 로그인이 만료됐어요. `shortform-uploader setup tiktok`으로 다시 연결해 주세요.",
        "reached_active_user_cap": "TikTok 앱의 하루 사용자 한도에 도달했어요. 내일 다시 시도해 주세요.",
    }
    return messages.get(code, f"TikTok이 게시를 거부했어요 (오류 코드: {code})")
