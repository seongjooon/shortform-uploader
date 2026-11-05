"""`shortform-uploader setup`: a patient, friendly guide that stays with the user until each account connects."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auth import AuthManager, AuthRequired, TokenStore
from .interactive import ICONS, LABELS, PLATFORMS, clean_dropped_path, copy_to_clipboard as _copy_to_clipboard

InputFunc = Callable[[str], str]
OutFunc = Callable[[str], None]
Updates = dict[str, str]
Validator = Callable[[str, dict[str, Any]], tuple[Updates | None, str | None]]

META_PERMISSIONS = "instagram_basic, instagram_content_publish, pages_read_engagement, pages_show_list, business_management"
INSTAGRAM_REDIRECT = "http://localhost/"
TIKTOK_DEFAULT_REDIRECT = "https://example.com/tiktok/callback"


class SetupPaused(Exception):
    """The user chose to stop; progress is already saved."""


@dataclass
class Problem:
    title: str
    steps: list[str]
    url: str | None = None


class CallbackProblem(Exception):
    def __init__(self, problem: Problem) -> None:
        super().__init__(problem.title)
        self.problem = problem


# ---------------------------------------------------------------- validators
# Error strings are shown right after "앗," so they read as a gentle explanation plus a fix.


def _youtube_client(raw: str, _creds: dict[str, Any]) -> tuple[Updates | None, str | None]:
    path = clean_dropped_path(raw)
    if raw and path.suffix.lower() == ".json":
        if not path.is_file():
            return None, f"그 위치에서 파일을 찾지 못했어요 ({path}). 다운로드 폴더에서 파일을 다시 끌어다 놓아 주세요."
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            client = data.get("installed") or data.get("web") or {}
            return {"client_id": client["client_id"], "client_secret": client["client_secret"]}, None
        except (ValueError, KeyError, TypeError, AttributeError):
            return None, ("Google에서 받은 클라이언트 파일이 아닌 것 같아요. "
                          "클라이언트를 만든 직후 창의 'JSON 다운로드' 버튼으로 받은 파일인지 확인해 주세요.")
    if raw.endswith(".apps.googleusercontent.com"):
        return {"client_id": raw}, None
    if raw.startswith("GOCSPX-"):
        return None, "이건 '보안 비밀번호'예요. 지금은 그 위에 있는 '클라이언트 ID'(…apps.googleusercontent.com)가 필요해요."
    return None, "클라이언트 ID는 '.apps.googleusercontent.com'으로 끝나요. 다운로드한 JSON 파일을 그냥 끌어다 놓아도 돼요."


def _youtube_secret(raw: str, _creds: dict[str, Any]) -> tuple[Updates | None, str | None]:
    if raw.startswith("GOCSPX-") and len(raw) > 20:
        return {"client_secret": raw}, None
    return None, "보안 비밀번호는 'GOCSPX-'로 시작해요. 클라이언트 ID 바로 아래 칸의 값을 복사해 주세요."


def _digits(key: str, label: str, lengths: range) -> Validator:
    def validate(raw: str, _creds: dict[str, Any]) -> tuple[Updates | None, str | None]:
        if raw.isdigit() and len(raw) in lengths:
            return {key: raw}, None
        return None, f"{label}는 숫자로만 된 {lengths.start}~{lengths.stop - 1}자리예요. 앞뒤에 공백이나 다른 글자가 같이 복사됐는지 봐 주세요."
    return validate


def _hex_secret(key: str, label: str) -> Validator:
    def validate(raw: str, _creds: dict[str, Any]) -> tuple[Updates | None, str | None]:
        if re.fullmatch(r"[0-9a-f]{32}", raw):
            return {key: raw}, None
        if "•" in raw or "*" in raw:
            return None, f"{label}이 가려진 상태로 복사됐어요. 옆의 '표시(Show)'를 누른 뒤 드러난 값을 복사해 주세요."
        return None, f"{label}은 영문 소문자와 숫자 32자예요. '표시'를 눌러 전체 값을 복사해 주세요."
    return validate


def _tiktok_key(raw: str, _creds: dict[str, Any]) -> tuple[Updates | None, str | None]:
    if re.fullmatch(r"[a-z0-9]{12,32}", raw):
        return {"client_key": raw}, None
    return None, "Client key는 영문 소문자와 숫자로 된 값이에요. 아래쪽 'Client secret'이 아니라 위쪽 'Client key'를 복사했는지 봐 주세요."


def _tiktok_secret(raw: str, creds: dict[str, Any]) -> tuple[Updates | None, str | None]:
    if raw == creds.get("client_key"):
        return None, "방금 넣은 Client key와 같은 값이에요. 바로 아래 'Client secret' 줄의 복사 아이콘을 눌러 주세요."
    if re.fullmatch(r"[A-Za-z0-9]{20,64}", raw):
        return {"client_secret": raw}, None
    return None, "Client secret은 영문과 숫자로 된 긴 값이에요. 복사 아이콘을 눌러 전체를 복사해 주세요."


def _tiktok_redirect(raw: str, _creds: dict[str, Any]) -> tuple[Updates | None, str | None]:
    value = raw or TIKTOK_DEFAULT_REDIRECT
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return None, "TikTok은 https:// 로 시작하는 주소만 받아요. 잘 모르겠으면 그냥 Enter를 눌러 기본 주소를 쓰세요."
    return {"redirect_uri": value}, None


# ---------------------------------------------------------------- diagnosis


def diagnose_callback(platform: str, url: str, redirect_uri: str) -> Problem | None:
    """Explain an error carried in the URL the user pasted back; None when it looks like a success."""
    params = {k: v[-1] for k, v in parse_qs(urlparse(url).query).items()}
    error = params.get("error", "")
    kind = params.get("error_type", "") or params.get("error_reason", "")
    if not error and "code" in params:
        return None
    if error in ("access_denied", "user_denied") or kind == "user_denied":
        return Problem("승인 화면에서 '취소'가 눌린 것 같아요.", [
            "다시 시도하면 같은 화면이 나와요. 이번에는 마지막에 '계속' 또는 '허용(Authorize)'을 눌러 주세요.",
        ])
    if platform == "tiktok" and (kind == "redirect_uri" or "redirect" in error):
        return Problem("TikTok에 등록한 Redirect URI(돌아올 주소)와 지금 쓰는 주소가 조금 달라요.", [
            f"지금 쓰는 주소는 이거예요: {redirect_uri}",
            "개발자 페이지 → 내 앱 → 위쪽 'Sandbox' 탭 → Login Kit → Redirect URI 칸에 위 주소를 그대로 붙여 넣어 주세요.",
            "끝의 '/' 하나, http와 https 차이도 다른 주소로 봐요. 복사해서 붙여 넣는 게 가장 안전해요.",
            "붙여 넣은 뒤 오른쪽 위 빨간 'Apply changes' 버튼을 꼭 눌러 주세요. 안 누르면 저장이 안 돼요.",
        ], "https://developers.tiktok.com/apps")
    if platform == "tiktok" and (error == "unauthorized_client" or kind == "client_key"):
        return Problem("TikTok이 아직 이 앱을 쓸 수 있게 열어 주지 않았어요.", [
            "심사 전인 앱은 'Sandbox'(심사 전 연습 공간)의 키로만 동작해요. Sandbox 탭의 Client key/secret을 쓰셨는지 확인해 주세요. 보통 'sb'로 시작해요.",
            "Sandbox → Sandbox settings → Target users에 업로드할 TikTok 계정이 들어 있는지 확인해 주세요.",
            "Sandbox에도 Login Kit과 Content Posting API가 추가돼 있어야 해요. 바꾼 게 있으면 'Apply changes'를 눌러 주세요.",
            "키를 잘못 넣은 것 같으면 아래에서 'r'을 눌러 다시 넣을 수 있어요.",
        ], "https://developers.tiktok.com/apps")
    if "scope" in kind or "scope" in error:
        return Problem("앱에 필요한 권한이 아직 켜져 있지 않아요.", [
            "개발자 페이지 → 내 앱 → Scopes 메뉴에 user.info.basic, video.upload, video.publish 세 개가 있는지 확인해 주세요.",
            "video.publish가 목록에 없으면 Content Posting API의 'Direct Post' 스위치를 켜면 나타나요.",
            "추가했으면 'Apply changes'를 눌러 주세요.",
        ], "https://developers.tiktok.com/apps")
    if error:
        return Problem(f"플랫폼이 이런 메시지를 돌려줬어요: {error} {kind}".strip(), [
            "앱 정보가 바뀌었을 수 있어요. 'r'을 눌러 키를 다시 넣어 보거나, 잠시 후 다시 시도해 주세요.",
        ])
    return Problem("붙여 넣은 주소에 로그인 결과가 들어 있지 않아요.", [
        "승인을 끝낸 뒤 바뀐 주소창의 주소 전체를 복사했는지 확인해 주세요. 주소에 'code='가 들어 있어야 해요.",
    ])


def diagnose(platform: str, message: str, creds: dict[str, Any]) -> Problem:
    """Turn an authentication failure into plain next steps."""
    if "no Instagram professional account is linked" in message:
        return Problem("Facebook 페이지에 Instagram이 아직 연결되지 않았어요. 가장 많이 막히는 부분이라 같이 해 봐요.", [
            "Facebook 오른쪽 위 프로필 사진을 누르고, 내 '페이지'로 전환해 주세요 (개인 프로필이 아니라 페이지예요).",
            "페이지 상태에서 설정 → '연결된 계정(Linked accounts)' → Instagram → '계정 연결'을 눌러 주세요.",
            "'이미 다른 페이지에 연결됨'이라고 나오면 'Switch Page'를 고르고 확인을 눌러 주세요.",
            "Instagram이 개인 계정이면 먼저 Instagram 앱 → 설정 → 계정 유형 및 도구 → '프로페셔널 계정으로 전환'을 해 주세요. 무료예요.",
            "다 됐으면 Facebook을 다시 개인 프로필로 전환한 뒤, 여기서 Enter로 다시 시도해 주세요.",
        ], "https://www.facebook.com/settings/?tab=linked_instagram")
    if "multiple Instagram accounts" in message:
        return Problem("연결된 Instagram 계정이 여러 개라 어느 쪽에 올릴지 정할 수 없어요.", [
            "업로드에 쓸 계정 하나만 남기고, 다른 페이지의 Instagram 연결은 잠시 해제한 뒤 다시 시도해 주세요.",
        ])
    if "registered redirect URI" in message:
        return Problem("붙여 넣은 주소가 등록해 둔 '돌아올 주소'로 시작하지 않아요.", [
            f"이 주소로 시작해야 해요: {creds.get('redirect_uri', '')}",
            "승인을 끝낸 뒤 열린 탭의 주소를 복사했는지 확인해 주세요.",
        ])
    if "state did not match" in message:
        return Problem("예전에 시도했던 주소가 붙여 넣어진 것 같아요.", [
            "이번에 새로 열린 창에서 승인한 뒤, 그 창의 주소를 붙여 넣어 주세요.",
        ])
    if "callback was not received" in message:
        return Problem("3분 안에 로그인이 끝나지 않았어요. 천천히 다시 해도 괜찮아요.", [
            "브라우저에서 업로드할 YouTube 채널의 Google 계정으로 로그인해 주세요.",
            "'Google에서 확인하지 않은 앱'이라는 화면이 나오면 정상이에요. 왼쪽 아래 '고급' → '(앱 이름)(으)로 이동'을 눌러 주세요.",
            "'액세스 차단됨'이 나오면 테스트 사용자 등록이 빠진 거예요. 바로가기를 열어 '테스트 사용자'에 본인 이메일을 추가해 주세요.",
        ], "https://console.cloud.google.com/auth/audience")
    if "rejected" in message or "authorization was declined" in message:
        return Problem("로그인 결과를 확인하는 데 실패했어요.", [
            "로그인 결과는 몇 분이 지나면 만료돼요. 승인한 뒤 바로 주소를 붙여 넣어 주세요.",
            "계속 같은 문제가 생기면 비밀번호(secret)가 틀렸을 수 있어요. 'r'을 눌러 다시 넣어 주세요.",
        ])
    if "could not be reached" in message:
        return Problem("인터넷 연결이 잠깐 끊긴 것 같아요.", ["와이파이를 확인한 뒤 Enter로 다시 시도해 주세요."])
    return Problem(f"연결하다가 문제가 생겼어요 ({message}).", [
        "'r'을 눌러 넣은 값을 다시 확인하거나, 잠시 후 Enter로 다시 시도해 주세요.",
    ])


# ---------------------------------------------------------------- steps


@dataclass
class Step:
    id: str
    title: str
    why: str
    actions: list[str]
    success: str = ""
    url: str | None = None
    copy: str | None = None
    help: list[str] = field(default_factory=list)
    prompt: str | None = None
    validate: Validator | None = None
    skip_if: Callable[[dict[str, Any], set[str]], bool] | None = None

    @property
    def is_field(self) -> bool:
        return self.validate is not None


@dataclass(frozen=True)
class Intro:
    why: str
    minutes: int
    needs: list[str]


INTROS = {
    "youtube": Intro(
        "YouTube에 대신 올리려면, Google에 '이 프로그램이 내 채널에 영상을 올려도 돼요'라고 한 번 등록해야 해요.",
        10, ["업로드할 YouTube 채널의 Google 계정"],
    ),
    "instagram": Intro(
        "Instagram은 Facebook 페이지를 거쳐서만 업로드를 허용해요. 그래서 Facebook 쪽 준비가 조금 필요해요.",
        15, ["Instagram 앱이 있는 휴대폰", "Facebook 계정 (페이지가 없으면 중간에 같이 만들어요)"],
    ),
    "tiktok": Intro(
        "TikTok에 캡션·해시태그와 함께 바로 게시하거나, 앱의 '받은편지함'으로 보내도록 연결해요.",
        10, ["업로드할 TikTok 계정", "개발자 페이지 가입에 쓸 이메일"],
    ),
}


def _youtube_steps() -> list[Step]:
    return [
        Step("project", "Google Cloud 프로젝트 만들기",
             "Google에 이 프로그램을 등록할 '폴더'를 하나 만드는 거예요.",
             ["업로드할 YouTube 채널의 Google 계정으로 로그인해 주세요.",
              "프로젝트 이름에 아무 이름(예: shortform-uploader)이나 넣고 '만들기'를 눌러 주세요."],
             "오른쪽 위에 '프로젝트를 만들었습니다' 알림이 뜨면 성공이에요.",
             "https://console.cloud.google.com/projectcreate",
             help=["이미 만들어 둔 프로젝트가 있으면 그걸 써도 돼요.", "결제 정보나 카드 등록은 필요 없어요."]),
        Step("api", "YouTube 업로드 기능 켜기",
             "방금 만든 프로젝트에서 YouTube 기능을 쓸 수 있게 스위치를 켜는 거예요.",
             ["화면 맨 위에 방금 만든 프로젝트 이름이 보이는지 확인해 주세요.",
              "파란색 '사용(Enable)' 버튼을 눌러 주세요."],
             "버튼이 '관리(Manage)'로 바뀌면 성공이에요.",
             "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
             help=["처음부터 '관리' 버튼이 보이면 이미 켜져 있는 거예요. 그냥 Enter를 누르세요.",
                   "다른 프로젝트가 선택돼 있으면 맨 위 프로젝트 이름을 눌러 바꿔 주세요."]),
        Step("consent", "로그인 화면 설정과 테스트 사용자 등록",
             "나중에 로그인할 때 보일 화면을 만들고, 내 계정을 '써도 되는 사람'으로 등록하는 거예요.",
             ["'시작하기'를 누르고 앱 이름(예: shortform-uploader)과 내 이메일을 넣어 주세요.",
              "대상(사용자 유형)은 '외부(External)'를 골라 주세요.",
              "왼쪽 메뉴 '대상(Audience)' → '테스트 사용자' → '사용자 추가'에 업로드할 Google 계정 이메일을 넣어 주세요."],
             "테스트 사용자 목록에 내 이메일이 보이면 성공이에요.",
             "https://console.cloud.google.com/auth/overview",
             help=["테스트 사용자를 빼먹으면 나중에 '액세스 차단됨' 화면이 나와요. 이 단계가 제일 중요해요.",
                   "'앱 게시'나 '검증 신청'은 하지 않아도 돼요."]),
        Step("client", "프로그램용 열쇠(클라이언트) 만들기",
             "이 프로그램이 Google에 '나 shortform-uploader야'라고 알릴 때 쓰는 열쇠를 받는 거예요.",
             ["애플리케이션 유형에서 '데스크톱 앱'을 골라 주세요.",
              "이름은 그대로 두고 '만들기'를 눌러 주세요.",
              "완료 창의 'JSON 다운로드'를 누르고, 받은 파일을 이 터미널 창으로 끌어다 놓은 뒤 Enter를 눌러 주세요."],
             "",
             "https://console.cloud.google.com/auth/clients/create",
             help=["'웹 애플리케이션'이 아니라 꼭 '데스크톱 앱'이어야 해요.",
                   "완료 창을 닫아 버렸다면 클라이언트 목록에서 방금 만든 항목을 누르면 다시 볼 수 있어요.",
                   "파일 대신 '클라이언트 ID'를 복사해 붙여 넣어도 돼요."],
             prompt="파일을 끌어다 놓거나 클라이언트 ID를 붙여 넣어 주세요:", validate=_youtube_client),
        Step("secret", "열쇠의 비밀번호",
             "열쇠와 짝이 되는 비밀번호예요. 파일을 끌어다 놓았다면 이 단계는 건너뛰어요.",
             ["같은 화면에서 '클라이언트 보안 비밀번호'(GOCSPX-로 시작) 옆 복사 버튼을 눌러 붙여 넣어 주세요."],
             help=["클라이언트 목록에서 방금 만든 항목을 누르면 오른쪽에 보여요."],
             prompt="보안 비밀번호를 붙여 넣어 주세요:", validate=_youtube_secret,
             skip_if=lambda _creds, filled: "client_secret" in filled),
    ]


def _instagram_steps() -> list[Step]:
    return [
        Step("professional", "Instagram을 프로페셔널 계정으로 바꾸기",
             "Instagram은 비즈니스/크리에이터 계정만 외부 프로그램 업로드를 허용해요.",
             ["휴대폰 Instagram 앱 → 내 프로필 → 오른쪽 위 ☰ → 설정을 열어 주세요.",
              "'계정 유형 및 도구' → '프로페셔널 계정으로 전환'을 눌러 안내대로 진행해 주세요."],
             "프로필에 '대시보드'나 '프로페셔널 대시보드'가 보이면 성공이에요.",
             help=["이미 비즈니스나 크리에이터 계정이면 그냥 Enter를 누르세요.",
                   "무료이고, 언제든 개인 계정으로 되돌릴 수 있어요."]),
        Step("page", "Facebook 페이지에 Instagram 연결하기 (가장 중요해요)",
             "Instagram 업로드는 Facebook 페이지를 통해서 이뤄져요. 여기서 많이 막히니 천천히 해 봐요.",
             ["Facebook 페이지가 없다면 지금 열린 화면에서 하나 만들어 주세요. 이름은 아무거나 괜찮아요.",
              "Facebook 오른쪽 위 프로필 사진 → 내 '페이지'로 전환해 주세요.",
              "페이지 상태에서 설정 → '연결된 계정' → Instagram → '계정 연결'을 눌러 주세요.",
              "연결이 끝나면 다시 개인 프로필로 전환해 주세요."],
             "페이지 설정의 Instagram 항목에 내 Instagram 아이디(@…)가 보이면 성공이에요.",
             "https://www.facebook.com/pages/create",
             help=["Meta 계정 센터에 Instagram을 추가한 것만으로는 부족해요. 꼭 '페이지 설정'에서 연결해야 해요.",
                   "페이지로 전환한 상태에서 이 주소를 열면 바로 연결 화면이 나와요: https://www.facebook.com/settings/?tab=linked_instagram",
                   "'이미 다른 페이지에 연결됨'이라고 나오면 'Switch Page'를 골라 주세요."]),
        Step("app", "Meta 개발자 앱 만들기",
             "이 프로그램을 Meta(Facebook·Instagram 회사)에 등록하는 거예요.",
             ["'앱 만들기'를 눌러 주세요. 처음이면 개발자 가입 화면이 먼저 나와요.",
              "사용 사례는 Facebook 로그인이나 페이지 관리와 관련된 항목을 골라 주세요.",
              "앱이 만들어지면 제품 목록에서 'Facebook Login for Business'를 찾아 '설정'을 눌러 추가해 주세요."],
             "왼쪽 메뉴에 'Facebook Login for Business'가 보이면 성공이에요.",
             "https://developers.facebook.com/apps/creation/",
             help=["개발자 가입 때 휴대폰 인증을 요구할 수 있어요.",
                   "비즈니스 포트폴리오를 연결하라는 화면은 건너뛰어도 돼요."]),
        Step("redirect", "로그인 후 돌아올 주소 등록하기",
             "로그인이 끝나면 이 주소로 돌아오게 돼요. 주소는 이미 복사해 뒀어요.",
             ["왼쪽 메뉴 Facebook Login for Business → 설정을 열어 주세요.",
              "'유효한 OAuth 리디렉션 URI' 칸에 Cmd+V로 붙여 넣고 '변경 내용 저장'을 눌러 주세요."],
             "칸에 http://localhost/ 가 남아 있으면 성공이에요.",
             copy=INSTAGRAM_REDIRECT,
             help=["끝의 '/'까지 똑같아야 해요. 복사된 값을 그대로 붙여 넣는 게 가장 안전해요."]),
        Step("config", "로그인 구성 만들기",
             "로그인할 때 어떤 권한을 받을지 묶어 두는 설정이에요. 필요한 권한 목록은 복사해 뒀어요.",
             ["Facebook Login for Business → '구성(Configurations)' → '구성 만들기'를 눌러 주세요.",
              "권한 고르는 화면에서 아래 5개를 모두 체크해 주세요.",
              f"   {META_PERMISSIONS}",
              "저장하면 목록에 숫자로 된 '구성 ID'가 생겨요. 그 숫자를 복사해 붙여 넣어 주세요."],
             copy=META_PERMISSIONS,
             help=["권한 이름을 검색창에 하나씩 붙여 넣으면 찾기 쉬워요.",
                   "구성 ID는 구성 이름 옆이나 아래에 있는 긴 숫자예요."],
             prompt="구성 ID를 붙여 넣어 주세요:", validate=_digits("config_id", "구성 ID", range(10, 21))),
        Step("app_id", "앱 ID 복사하기",
             "내 앱의 번호예요.",
             ["왼쪽 메뉴 '앱 설정' → '기본 설정'을 열고 '앱 ID'를 복사해 붙여 넣어 주세요."],
             help=["화면 맨 위 앱 이름 옆에도 앱 ID가 보여요."],
             prompt="앱 ID를 붙여 넣어 주세요:", validate=_digits("client_id", "앱 ID", range(10, 21))),
        Step("app_secret", "앱 시크릿 코드 복사하기",
             "앱의 비밀번호예요. 이 PC에만 안전하게 저장되고 다른 곳으로 보내지 않아요.",
             ["같은 화면의 '앱 시크릿 코드' 옆 '표시'를 누르고, 드러난 값을 복사해 붙여 넣어 주세요."],
             help=["'표시'를 누르면 Facebook 비밀번호를 한 번 더 물어볼 수 있어요."],
             prompt="앱 시크릿 코드를 붙여 넣어 주세요:", validate=_hex_secret("client_secret", "앱 시크릿 코드")),
    ]


def _tiktok_steps() -> list[Step]:
    return [
        Step("app", "TikTok 개발자 앱 만들기",
             "이 프로그램을 TikTok에 등록하는 거예요.",
             ["TikTok for Developers에 로그인(처음이면 가입)해 주세요.",
              "'Connect an app' → 'Individual' → 앱 이름(예: shortform-uploader) 입력 → 'Create app'을 눌러 주세요.",
              "왼쪽 'Products' → 'Add products'에서 'Login Kit'과 'Content Posting API'를 추가해 주세요."],
             "Products 아래에 Login Kit과 Content Posting API가 보이면 성공이에요.",
             "https://developers.tiktok.com/apps",
             help=["개발자 가입 계정과 업로드할 TikTok 계정은 달라도 괜찮아요.",
                   "앱 아이콘, 약관 주소 같은 칸은 나중에 심사받을 때만 필요해요. 지금은 비워 두세요."]),
        Step("sandbox", "연습 공간(Sandbox) 만들고 내 계정 추가하기",
             "심사 전인 앱은 'Sandbox'라는 연습 공간에서만 로그인이 돼요. 여기에 내 TikTok 계정을 등록해요.",
             ["앱 화면 위쪽 'Production / Sandbox' 중 'Sandbox'를 누르고 'Create Sandbox'를 눌러 주세요.",
              "Sandbox 안에서도 'Add products'로 Login Kit과 Content Posting API를 다시 추가해 주세요.",
              "Content Posting API 아래 'Direct Post' 스위치를 켜 주세요. 캡션과 해시태그까지 자동으로 넣어 게시하려면 필요해요.",
              "아래쪽 'Target users' → 'Add account'를 누르고, 업로드할 TikTok 계정으로 로그인해 주세요."],
             "Target users 표에 내 TikTok 아이디가 보이면 성공이에요.",
             "https://developers.tiktok.com/apps",
             help=["Production과 Sandbox는 설정이 따로예요. 지금부터는 Sandbox 쪽에서 해 주세요.",
                   "Target users에 없는 계정은 나중에 'unauthorized_client' 오류가 나요."]),
        Step("redirect_uri", "로그인 후 돌아올 주소 정하기",
             "TikTok 로그인이 끝나면 이 주소로 돌아와요. 서버가 없어도 괜찮아요. 주소창만 복사하면 되거든요.",
             [f"잘 모르겠으면 그냥 Enter를 누르세요. 기본 주소({TIKTOK_DEFAULT_REDIRECT})를 쓸게요.",
              "직접 가진 https 주소가 있으면 그 주소를 붙여 넣어도 돼요."],
             help=["돌아온 페이지 내용은 상관없어요. 'Example Domain' 같은 페이지가 보여도 정상이에요.",
                   "안전한가요? 이 주소에 실리는 값은 한 번만, 몇 분만 쓸 수 있고 이 PC에 있는 정보가 있어야만 쓸 수 있어요."],
             prompt="주소 [Enter: 기본 주소]:", validate=_tiktok_redirect),
        Step("register", "돌아올 주소 등록하기",
             "방금 정한 주소를 TikTok에 알려 주는 거예요. 주소는 이미 복사해 뒀어요.",
             ["Sandbox 화면 → Products → Login Kit → 'Redirect URI' 칸에 Cmd+V로 붙여 넣어 주세요.",
              "Scopes 메뉴에 user.info.basic, video.upload, video.publish 세 개가 있는지도 확인해 주세요.",
              "오른쪽 위 빨간 'Apply changes' 버튼을 꼭 눌러 주세요."],
             "'Apply changes' 버튼이 회색으로 바뀌면 저장된 거예요.",
             copy="{redirect_uri}",
             help=["끝의 '/'까지 똑같아야 해요.", "'Apply changes'를 안 누르면 저장이 안 돼서 나중에 주소 오류가 나요."]),
        Step("client_key", "Client key 복사하기",
             "내 앱의 아이디예요.",
             ["Sandbox 화면 맨 위 'Credentials'에서 'Client key' 줄의 복사 아이콘을 눌러 붙여 넣어 주세요."],
             help=["꼭 Sandbox 탭의 값이어야 해요. Sandbox 키는 보통 'sb'로 시작해요.",
                   "값이 ●●●로 가려져 있어도 복사 아이콘을 누르면 실제 값이 복사돼요."],
             prompt="Client key를 붙여 넣어 주세요:", validate=_tiktok_key),
        Step("client_secret", "Client secret 복사하기",
             "앱의 비밀번호예요. 이 PC에만 안전하게 저장돼요.",
             ["바로 아래 'Client secret' 줄의 복사 아이콘을 눌러 붙여 넣어 주세요."],
             help=["Client key와 다른 값이어야 해요."],
             prompt="Client secret을 붙여 넣어 주세요:", validate=_tiktok_secret),
    ]


STEPS: dict[str, Callable[[], list[Step]]] = {
    "youtube": _youtube_steps, "instagram": _instagram_steps, "tiktok": _tiktok_steps,
}
_FIXED = {"instagram": {"redirect_uri": INSTAGRAM_REDIRECT, "graph_version": "v24.0"}}


# ---------------------------------------------------------------- persistence


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Progress:
    """credentials.json (merged per platform) plus which guide steps were finished."""

    def __init__(self, store: TokenStore) -> None:
        self.cred_path = store.credentials_path
        self.state_path = store.directory / "setup_state.json"

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def creds(self, platform: str) -> dict[str, Any]:
        return dict(self._read(self.cred_path).get(platform) or {})

    def save_creds(self, platform: str, updates: dict[str, Any]) -> None:
        data = self._read(self.cred_path)
        data[platform] = {**(data.get(platform) or {}), **updates}
        _atomic_write(self.cred_path, data)

    def done_steps(self, platform: str) -> list[str]:
        return list(self._read(self.state_path).get(platform) or [])

    def mark(self, platform: str, step_id: str | None) -> None:
        data = self._read(self.state_path)
        steps = list(data.get(platform) or [])
        if step_id is None:
            steps = []
        elif step_id not in steps:
            steps.append(step_id)
        data[platform] = steps
        _atomic_write(self.state_path, data)


# ---------------------------------------------------------------- coach




def _cheer(done: int, total: int) -> str:
    if done >= total:
        return "🙌 좋아요! 준비는 다 끝났어요. 이제 계정만 연결하면 돼요."
    if done * 2 >= total:
        return f"💪 잘하고 있어요! 절반 넘게 왔어요. ({done}/{total})"
    return f"👍 좋아요, 한 단계 끝! ({done}/{total})"


class Coach:
    def __init__(self, input_func: InputFunc, out: OutFunc, open_url: Callable[[str], Any], copy: Callable[[str], bool]):
        self.input, self.out, self._open, self._copy = input_func, out, open_url, copy

    def open(self, url: str) -> None:
        self.out(f"  🌐 필요한 페이지를 브라우저에 열어 둘게요: {url}")
        try:
            self._open(url)
        except Exception:  # a missing browser must not stop the guide; the URL is printed above
            self.out("     창이 안 열리면 위 주소를 복사해서 브라우저 주소창에 붙여 넣어 주세요.")

    def copy(self, value: str) -> None:
        if self._copy(value):
            self.out(f"  📋 붙여 넣을 값을 미리 복사해 뒀어요 (Cmd+V로 붙여 넣으세요): {value}")
        else:
            self.out(f"  📋 이 값을 복사해서 써 주세요: {value}")

    def show_help(self, lines: list[str]) -> None:
        self.out("  💡 이런 것들을 확인해 보세요")
        for line in lines or ["화면이 설명과 다르게 보이면, 화면에 보이는 버튼 이름을 기준으로 비슷한 메뉴를 찾아 주세요."]:
            self.out(f"     · {line}")

    def show_problem(self, problem: Problem) -> None:
        self.out("\n  🤗 괜찮아요, 처음 할 때 자주 생기는 일이에요. 같이 해결해 봐요.")
        self.out(f"  🔎 원인: {problem.title}")
        self.out("  🛠️  이렇게 해 보세요:")
        for idx, line in enumerate(problem.steps, 1):
            self.out(f"     {idx}. {line}")

    def _command(self, raw: str, help_lines: list[str]) -> bool:
        lowered = raw.strip().lower()
        if lowered in ("q", "quit", "그만", "나중에"):
            raise SetupPaused
        if lowered in ("?", "help", "도움", "도움말"):
            self.show_help(help_lines)
            return True
        return False

    def wait(self, help_lines: list[str], url: str | None) -> None:
        reopen = " · o: 페이지 다시 열기" if url else ""
        while True:
            raw = self.input(f"  다 하셨으면 Enter를 눌러 주세요.  (?: 막혔어요{reopen} · q: 나중에 이어서) ")
            if self._command(raw, help_lines):
                continue
            if raw.strip().lower() == "o" and url:
                self.open(url)
                continue
            return

    def ask(self, prompt: str, validate: Validator, creds: dict[str, Any], help_lines: list[str], url: str | None) -> Updates:
        failures = 0
        while True:
            raw = self.input(f"  👉 {prompt} ").strip()
            if self._command(raw, help_lines):
                continue
            if raw.lower() == "o" and url:
                self.open(url)
                continue
            updates, error = validate(raw, creds)
            if error is None and updates is not None:
                self.out("  ✓ 좋아요, 확인했어요.")
                return updates
            failures += 1
            self.out(f"  앗, {error}")
            if failures % 3 == 0:
                self.out("  🫶 여러 번 막혔네요. 괜찮아요, 천천히 같이 확인해 봐요.")
                self.show_help(help_lines)
                if url:
                    self.out("  해당 페이지를 다시 열려면 'o'를 입력하세요.")


# ---------------------------------------------------------------- wizard


def _connect(platform: str, coach: Coach, progress: Progress, auth_factory, store) -> str:
    """Run OAuth until it works. Returns 'done' or 'rekey' (user wants to re-enter app values)."""
    while True:
        creds = progress.creds(platform)
        redirect = str(creds.get("redirect_uri", ""))

        def paste(_prompt: str) -> str:
            coach.out("  승인을 끝내면 브라우저 주소창의 주소가 바뀌어요.")
            coach.out(f"  그 주소 전체(예: {redirect or 'https://…'}?code=…)를 복사해서 여기에 붙여 넣어 주세요.")
            help_lines = [
                "돌아온 페이지가 '연결할 수 없음'이나 'Example Domain'으로 보여도 정상이에요. 주소창만 복사하면 돼요.",
                "주소창을 한 번 클릭하고 Cmd+A, Cmd+C를 누르면 전체가 복사돼요.",
                "TikTok에서 'Maximum number of attempts reached'가 보이면 본인 확인을 너무 여러 번 시도한 거예요. "
                "Resend를 더 누르지 말고, q로 멈췄다가 몇 시간 뒤 다시 실행해 주세요. 진행한 내용은 저장돼요.",
            ]
            while True:
                raw = coach.input("  👉 주소: ").strip()
                if coach._command(raw, help_lines):
                    continue
                if not raw.startswith("http"):
                    coach.out("  앗, http로 시작하는 주소 전체를 붙여 넣어 주세요. 주소창을 클릭하고 Cmd+A, Cmd+C로 복사하면 돼요.")
                    continue
                host = urlparse(raw).netloc
                if redirect and not raw.startswith(redirect.rstrip("/")) and host.endswith(("tiktok.com", "facebook.com")):
                    # TikTok reports app misconfiguration on its own authorize page URL.
                    problem = diagnose_callback(platform, raw, redirect) if "error=" in raw else None
                    if problem:
                        raise CallbackProblem(problem)
                    coach.out("  앗, 아직 승인 화면의 주소예요. 화면의 '계속'이나 '허용'을 누른 뒤, 바뀐 주소를 복사해 주세요.")
                    continue
                problem = diagnose_callback(platform, raw, redirect)
                if problem:
                    raise CallbackProblem(problem)
                return raw

        coach.out(f"\n━━ 🔐 마지막 단계: {ICONS[platform]} {LABELS[platform]} 계정 연결 ━━")
        coach.out("  곧 브라우저가 열려요. 영상을 올릴 계정으로 로그인하고, 마지막 화면에서 '허용'을 눌러 주세요.")
        try:
            auth_factory(platform, store=store, input_func=paste, browser_open=coach.open).authorize()
        except CallbackProblem as exc:
            problem = exc.problem
        except AuthRequired as exc:
            problem = diagnose(platform, str(exc), creds)
        else:
            account = (store.load(platform) or {}).get("account_id", "")
            coach.out(f"\n  🎉 {LABELS[platform]} 연결 완료! 수고 많으셨어요. (계정 {account})")
            return "done"
        coach.show_problem(problem)
        if problem.url:
            coach.out(f"     바로가기: {problem.url}")
        while True:
            reopen = " · o: 바로가기 열기" if problem.url else ""
            choice = coach.input(
                f"  고치셨으면 Enter로 다시 시도해요.  (r: 키 다시 입력{reopen} · ?: 다시 보기 · q: 나중에 이어서) "
            ).strip().lower()
            if coach._command(choice, problem.steps):
                continue
            if choice == "o" and problem.url:
                coach.open(problem.url)
                continue
            if choice == "r":
                return "rekey"
            break


def _show_intro(platform: str, steps: list[Step], coach: Coach) -> None:
    intro = INTROS[platform]
    coach.out(f"\n━━ {ICONS[platform]} {LABELS[platform]} 연결을 시작할게요 ━━")
    coach.out(f"  {intro.why}")
    coach.out(f"  ⏱  약 {intro.minutes}분, {len(steps)}단계 + 마지막 계정 연결")
    coach.out(f"  🎒 준비물: {', '.join(intro.needs)}")


def _show_step(platform: str, index: int, steps: list[Step], step: Step, coach: Coach) -> None:
    coach.out(f"\n📍 [{LABELS[platform]} {index + 1}/{len(steps)}] {step.title}")
    coach.out(f"  💭 왜 하나요? {step.why}")
    coach.out("  🛠️  이렇게 하세요:")
    number = 0
    for line in step.actions:
        if line.startswith("   "):
            coach.out(f"     {line.strip()}")
        else:
            number += 1
            coach.out(f"    {number}. {line}")
    if step.success:
        coach.out(f"  ✅ {step.success}")


_APP_KEYS = {
    "youtube": ("client_id", "client_secret"),
    "instagram": ("config_id", "client_id", "client_secret"),
    "tiktok": ("redirect_uri", "client_key", "client_secret"),
}


def _has_app_values(platform: str, creds: dict[str, Any]) -> bool:
    return all(creds.get(key) for key in _APP_KEYS[platform])


def setup_platform(platform: str, coach: Coach, progress: Progress, auth_factory, store) -> None:
    steps = STEPS[platform]()
    if platform in _FIXED:
        progress.save_creds(platform, _FIXED[platform])
    done = progress.done_steps(platform)
    if done:
        answer = coach.input(
            f"\n{LABELS[platform]}: 지난번에 {len(done)}/{len(steps)}단계까지 하셨어요. 거기서부터 이어서 할까요? [Y/n] "
        ).strip().lower()
        if answer in ("n", "no"):
            progress.mark(platform, None)
            done = []
    index = 0
    if not done and _has_app_values(platform, progress.creds(platform)):
        answer = coach.input(
            f"\n{LABELS[platform]} 앱 정보는 이미 입력돼 있어요. 앱 설정 안내는 건너뛰고 바로 계정 연결로 갈까요? [Y/n] "
        ).strip().lower()
        if answer not in ("n", "no"):
            index = len(steps)
    if not done and index == 0:
        _show_intro(platform, steps, coach)
    filled: set[str] = set()
    while index <= len(steps):
        if index == len(steps):
            if _connect(platform, coach, progress, auth_factory, store) == "done":
                progress.mark(platform, None)
                return
            index = next(i for i, s in enumerate(steps) if s.is_field)
            progress.mark(platform, None)
            done, filled = [], set()
            coach.out("\n  좋아요, 넣었던 값을 처음부터 하나씩 다시 확인해 볼게요.")
            continue
        step = steps[index]
        creds = progress.creds(platform)
        if step.id in done or (step.skip_if and step.skip_if(creds, filled)):
            index += 1
            continue
        _show_step(platform, index, steps, step, coach)
        if step.url:
            coach.open(step.url)
        if step.copy:
            try:
                coach.copy(step.copy.format_map({k: str(v) for k, v in creds.items()}))
            except KeyError:
                coach.copy(step.copy)
        if step.is_field:
            updates = coach.ask(step.prompt or "", step.validate, creds, step.help, step.url)
            progress.save_creds(platform, updates)
            filled.update(updates)
        else:
            coach.wait(step.help, step.url)
        progress.mark(platform, step.id)
        done = progress.done_steps(platform)
        coach.out(f"  {_cheer(index + 1, len(steps))}")
        index += 1


def run_setup(
    platforms: list[str] | None = None,
    *,
    store: TokenStore | None = None,
    input_func: InputFunc = input,
    out: OutFunc = print,
    open_url: Callable[[str], Any] = webbrowser.open,
    copy: Callable[[str], bool] = _copy_to_clipboard,
    auth_factory=AuthManager,
) -> int:
    store = store or TokenStore()
    coach = Coach(input_func, out, open_url, copy)
    progress = Progress(store)
    out("안녕하세요! shortform-uploader 계정 연결을 도와드릴게요. 😊")
    out("처음 한 번만 하면 되고, 플랫폼마다 10~15분 정도 걸려요.")
    out("화면에 나오는 순서대로 하나씩 따라 하시면 돼요. 실수해도 괜찮아요, 언제든 다시 할 수 있어요.")
    out("  · 막히면 언제든 '?'를 입력하세요. 확인할 것들을 알려 드려요.")
    out("  · 'q'로 멈추면 지금까지 한 내용이 저장되고, 다음에 같은 곳부터 이어서 할 수 있어요.")
    try:
        if not platforms:
            from .interactive import select_platforms
            out("\n🔎 지금 연결 상태예요:")
            for p in PLATFORMS:
                state = "✅ 연결됨" if store.load(p) else "⚪ 아직 연결 안 됨"
                out(f"  {state}  {ICONS[p]} {LABELS[p]}")
            platforms = select_platforms(list(PLATFORMS), input_func, out)
        for platform in platforms:
            if store.load(platform):
                again = input_func(f"\n{LABELS[platform]}은 이미 연결돼 있어요. 다시 연결할까요? [y/N] ").strip().lower()
                if again not in ("y", "yes"):
                    continue
            setup_platform(platform, coach, progress, auth_factory, store)
    except SetupPaused:
        out("\n💾 알겠어요, 여기까지 저장해 뒀어요. 언제든 `shortform-uploader setup`을 다시 실행하면 이어서 할 수 있어요. 👋")
        return 0
    out("\n🎉 연결이 모두 끝났어요. 정말 수고 많으셨어요!")
    for p in PLATFORMS:
        out(f"  {'✅' if store.load(p) else '⚪'} {ICONS[p]} {LABELS[p]}")
    out("🚀 이제 터미널에 `shortform-uploader`를 입력하면 영상을 올릴 수 있어요.")
    return 0
