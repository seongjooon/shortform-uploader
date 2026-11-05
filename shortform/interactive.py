"""Guided terminal upload: drop a video, type a caption, pick platforms, get notified."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import pipeline

try:  # enables proper editing of multibyte (Korean) input in input(); absent on some platforms
    import readline  # noqa: F401
except ImportError:
    pass
from .models import FAILED, PUBLISHED, PlatformRecord, PlatformTarget
from .preflight import _probe, _video_details, check_video

PLATFORMS = ("youtube", "instagram", "tiktok")
LABELS = {"youtube": "YouTube Shorts", "instagram": "Instagram Reels", "tiktok": "TikTok"}
ICONS = {"youtube": "📺", "instagram": "📸", "tiktok": "🎵"}
_DEFAULT_PRIVACY = {"youtube": "private", "instagram": "account", "tiktok": "inbox"}
_YOUTUBE_TITLE_MAX = 100

InputFunc = Callable[[str], str]
OutFunc = Callable[[str], None]


def clean_dropped_path(raw: str) -> Path:
    """Normalize a path pasted by dragging a file onto a terminal window."""
    text = raw.strip()
    if text.startswith("file://"):
        return Path(unquote(urlparse(text).path))
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    else:
        # Terminal.app and iTerm escape spaces and shell metacharacters with backslashes.
        text = re.sub(r"\\(.)", r"\1", text)
    return Path(text).expanduser()


def parse_hashtags(raw: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,]+", raw):
        word = token.lstrip("#")
        if word and word.lower() not in seen:
            seen.add(word.lower())
            tags.append(f"#{word}")
    return tags


def compose_caption(caption: str, hashtags: list[str]) -> str:
    return "\n\n".join(part for part in (caption.strip(), " ".join(hashtags)) if part)


def default_title(caption: str, video: Path) -> str:
    first_line = next((line.strip() for line in caption.splitlines() if line.strip()), "")
    return (first_line or video.stem)[:_YOUTUBE_TITLE_MAX]


def describe_video(video: Path) -> list[str]:
    lines = [f"🎞️  파일: {video.name} ({video.stat().st_size / 1024 / 1024:.1f} MB)"]
    payload, warning = _probe(video)
    if warning:
        return lines + [warning]
    duration, width, height, fps = _video_details(payload or {})
    if duration is not None:
        lines.append(f"⏱️  길이: {duration:.1f}초")
    if width and height:
        orientation = "세로" if height > width else "가로" if width > height else "정사각형"
        lines.append(f"📐 해상도: {width}x{height} ({orientation})")
    if fps:
        lines.append(f"🎚️  프레임: {fps:.0f}fps")
    return lines


def copy_to_clipboard(value: str) -> bool:
    for command in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(command[0]):
            try:
                subprocess.run(command, input=value.encode(), timeout=5, check=True)
                return True
            except (OSError, subprocess.SubprocessError):
                return False
    return False


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification; the terminal output remains the source of truth."""
    sys.stdout.write("\a")
    sys.stdout.flush()
    try:
        if sys.platform == "darwin":
            # Text goes through argv so captions or errors can never be evaluated as AppleScript.
            subprocess.run(
                ["osascript", "-e", "on run argv", "-e",
                 "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                 title, message],
                capture_output=True, timeout=5, check=False,
            )
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def available_targets(config_path: Path | None, store) -> tuple[dict[str, PlatformTarget], dict[str, str]]:
    """Return publishable targets plus a reason for each platform that cannot be used."""
    tokens = {p: store.load(p) for p in PLATFORMS}
    unavailable: dict[str, str] = {}
    if config_path is not None:
        from .config import load_config
        configured = load_config(config_path)
        targets = {}
        for p in PLATFORMS:
            token = tokens[p]
            if p not in configured:
                unavailable[p] = "설정에서 꺼져 있음"
            elif not token:
                unavailable[p] = f"미연결 (shortform-uploader setup {p})"
            elif token.get("account_id") != configured[p].account_id:
                unavailable[p] = "연결된 계정이 설정의 account_id와 다름"
            else:
                targets[p] = configured[p]
        return targets, unavailable
    targets = {}
    for p in PLATFORMS:
        account_id = (tokens[p] or {}).get("account_id")
        if account_id:
            targets[p] = PlatformTarget(p, account_id, _DEFAULT_PRIVACY[p], {})
        else:
            unavailable[p] = f"미연결 (shortform-uploader setup {p})"
    return targets, unavailable


def select_platforms(names: list[str], input_func: InputFunc, out: OutFunc) -> list[str]:
    out("\n🌍 어디에 올릴까요?")
    for idx, name in enumerate(names, 1):
        out(f"  {idx}. {ICONS.get(name, '•')} {LABELS.get(name, name)}")
    while True:
        choice = input_func("번호를 쉼표로 구분해 입력 (예: 1,2 / Enter: 전체): ").strip().lower()
        if not choice or choice in ("a", "all"):
            return list(names)
        picked: list[str] = []
        for token in filter(None, (t.strip() for t in choice.split(","))):
            name = names[int(token) - 1] if token.isdigit() and 1 <= int(token) <= len(names) else token
            if name not in names:
                picked = []
                break
            if name not in picked:
                picked.append(name)
        if picked:
            return picked
        out("🤔 목록에 있는 번호로 입력해 주세요. (예: 1,3)")


def _video_problem(video: Path) -> str | None:
    if not video.is_file():
        return f"😢 파일을 찾을 수 없어요: {video}"
    if video.suffix.lower() != ".mp4":
        return "🙅 MP4 파일만 올릴 수 있어요."
    return None


def _ask_video(input_func: InputFunc, out: OutFunc) -> Path:
    while True:
        raw = input_func("\n🎬 영상 파일을 이 창에 끌어다 놓고 Enter를 눌러 주세요: ")
        if not raw.strip():
            continue
        video = clean_dropped_path(raw)
        problem = _video_problem(video)
        if problem is None:
            return video
        out(problem)


def _apply_metadata(target: PlatformTarget, text: str, title: str) -> PlatformTarget:
    metadata = {k: v for k, v in target.metadata.items() if k in ("made_for_kids", "share_to_feed")}
    if target.provider == "youtube":
        metadata.update(title=title, description=text)
    else:
        metadata["caption"] = text
    return PlatformTarget(target.provider, target.account_id, target.privacy, metadata)


_STATE_ICON = {PUBLISHED: "🎉", FAILED: "❌"}


def done_message(platform: str, rec: PlatformRecord, job_id: str) -> tuple[str, str]:
    label = LABELS.get(platform, platform)
    if rec.state == PUBLISHED and rec.delivery == "inbox":
        return f"{label} 전송 완료", "TikTok 앱 받은편지함에서 영상을 열고 게시하세요."
    if rec.state == PUBLISHED:
        message = rec.url or "게시되었습니다."
        return f"{label} 업로드 완료", f"{message}\n{rec.note}" if rec.note else message
    if rec.state == FAILED:
        return f"{label} 업로드 실패", rec.error or "원인을 확인하세요."
    return f"{label} 확인 필요", f"결과를 알 수 없습니다. shortform-uploader status {job_id}"


def run_wizard(
    *,
    publishers_factory: Callable[[Mapping[str, PlatformTarget]], Mapping],
    store,
    config_path: Path | None = None,
    jobs_dir: str | None = None,
    input_func: InputFunc = input,
    out: OutFunc = print,
    notifier: Callable[[str, str], None] = notify,
    video: str | None = None,
    copy: Callable[[str], bool] = copy_to_clipboard,
) -> int:
    out("🚀 shortform-uploader 업로드를 시작할게요!")
    targets, unavailable = available_targets(config_path, store)
    for p in PLATFORMS:
        mark = "✅" if p in targets else "⚪"
        out(f"  {mark} {ICONS[p]} {LABELS[p]}" + (f"  {unavailable[p]}" if p in unavailable else ""))
    if not targets:
        out("\n🔌 아직 연결된 계정이 없어요. 먼저 `shortform-uploader setup`으로 계정을 연결해 주세요. 단계별로 같이 할게요! 🙌")
        return 2

    given = clean_dropped_path(video) if video else None
    problem = _video_problem(given) if given else None
    if problem:
        out(f"\n{problem}")
    video = given if given and not problem else _ask_video(input_func, out)
    shown = describe_video(video)
    for line in shown:
        out(f"  {line}")

    caption = input_func("\n✍️  캡션: ").strip()
    hashtags = parse_hashtags(input_func("#️⃣  해시태그 (공백이나 쉼표로 구분, # 없이 써도 돼요): "))
    names = select_platforms(list(targets), input_func, out) if len(targets) > 1 else list(targets)

    text = compose_caption(caption, hashtags)
    title = ""
    if "youtube" in names:
        suggested = default_title(caption, video)
        title = input_func(f"📺 YouTube 제목 [Enter: {suggested}]: ").strip()[:_YOUTUBE_TITLE_MAX] or suggested
    selected = {p: _apply_metadata(targets[p], text, title) for p in names}

    out("\n📋 이렇게 올릴게요")
    out(f"  🎬 영상: {video}")
    out(f"  🌍 플랫폼: {', '.join(f'{ICONS[p]} {LABELS[p]}' for p in names)}")
    if title:
        out(f"  📺 YouTube 제목: {title}")
    out(f"  ✍️  캡션:\n{text or '(없음)'}")

    result = check_video(video, selected)
    for warning in result.warnings:
        if warning not in shown:
            out(f"⚠️  {warning}")
    blocked = {p: reasons for p, reasons in result.errors.items() if reasons}
    for p, reasons in blocked.items():
        out(f"🚫 {ICONS[p]} {LABELS[p]}에는 올릴 수 없어요: {'; '.join(reasons)}")
    selected = {p: t for p, t in selected.items() if p not in blocked}
    if not selected:
        return 2

    if input_func("\n👉 이대로 올릴까요? [y/N] ").strip().lower() not in ("y", "yes"):
        out("👌 알겠어요. 올리지 않고 마칠게요.")
        return 0

    job = pipeline.create_job(video, selected, jobs_dir)
    pipeline.approve(job, jobs_dir)

    def on_done(platform: str, rec: PlatformRecord) -> None:
        title_text, message = done_message(platform, rec, job.id)
        out(f"{_STATE_ICON.get(rec.state, '🔔')} [{title_text}] {message}")
        notifier(title_text, message)

    out("\n⏳ 업로드 중이에요. 영상 크기에 따라 몇 분 걸릴 수 있어요...")
    with pipeline.job_lock(job.id, jobs_dir):
        pipeline.run(job, publishers_factory(selected), jobs_dir, on_done=on_done)
    tiktok = job.records.get("tiktok")
    if tiktok and tiktok.state == PUBLISHED and tiktok.delivery == "inbox" and text:
        # TikTok's inbox upload API has no caption field, so the user pastes it in the app.
        copied = copy(text)
        out("\n🎵 TikTok 앱 받은편지함에서 영상을 열고, 캡션 칸에 붙여 넣어 주세요.")
        out("📋 캡션과 해시태그를 이 Mac의 클립보드에 복사해 뒀어요. "
            "같은 Apple 계정의 iPhone이고 Handoff가 켜져 있으면 휴대폰에서 바로 붙여 넣을 수 있어요:"
            if copied else "아래 문구를 복사해서 써 주세요:")
        out(text)
    return 0 if all(r.state == PUBLISHED for r in job.records.values()) and not blocked else 1
