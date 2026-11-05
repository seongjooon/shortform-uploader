"""Interface-independent operations shared by the terminal and future clients.

Calling upload means the caller has explicitly approved the supplied plan.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
import re

from . import pipeline, preflight
from .auth import AuthManager, TokenStore
from .models import Job, PlatformRecord, PlatformTarget
from .publishers.mock import MockPublisher

PLATFORMS = ("youtube", "instagram", "tiktok")
DEFAULT_PRIVACY = {"youtube": "private", "instagram": "account", "tiktok": "inbox"}


def accounts(store=None) -> dict[str, str | None]:
    store = store or TokenStore()
    return {p: (store.load(p) or {}).get("account_id") for p in PLATFORMS}


def available_targets(config_path, store):
    tokens = {p: store.load(p) for p in PLATFORMS}
    unavailable = {}
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
            targets[p] = PlatformTarget(p, account_id, DEFAULT_PRIVACY[p], {})
        else:
            unavailable[p] = f"미연결 (shortform-uploader setup {p})"
    return targets, unavailable


@dataclass(frozen=True)
class Inspection:
    size: int
    duration: float | None
    width: int | None
    height: int | None
    fps: float | None
    checks: preflight.PreflightResult


def inspect(path, targets: Mapping[str, PlatformTarget]) -> Inspection:
    video = Path(path)
    payload, _ = preflight._probe(video)
    duration, width, height, fps = preflight._video_details(payload or {})
    return Inspection(video.stat().st_size, duration, width, height, fps, preflight.check_video(video, targets))


def parse_hashtags(raw: str) -> list[str]:
    tags, seen = [], set()
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
    return (first_line or video.stem)[:100]


@dataclass(frozen=True)
class UploadPlan:
    video: Path
    targets: dict[str, PlatformTarget]


def plan(path, caption: str, hashtags: list[str], platforms: list[str], title: str, targets: Mapping[str, PlatformTarget]) -> UploadPlan:
    video = Path(path)
    if not platforms or len(set(platforms)) != len(platforms) or any(p not in targets for p in platforms):
        raise ValueError("게시 대상 플랫폼이 비었거나 올바르지 않습니다")
    text = compose_caption(caption, hashtags)
    title = title.strip()[:100] or default_title(caption, video)
    selected = {}
    for p in platforms:
        target = targets[p]
        metadata = {k: v for k, v in target.metadata.items() if k in ("made_for_kids", "share_to_feed")}
        if target.provider == "youtube":
            metadata.update(title=title, description=text)
        else:
            metadata["caption"] = text
        selected[p] = PlatformTarget(target.provider, target.account_id, target.privacy, metadata)
    return UploadPlan(video, selected)


def publishers(targets):
    names = {"youtube": "YouTubePublisher", "instagram": "InstagramPublisher", "tiktok": "TikTokPublisher"}
    result = {}
    for p, target in targets.items():
        if target.provider == "mock":
            result[p] = MockPublisher(p)
        else:
            result[p] = getattr(import_module(f"shortform.publishers.{target.provider}"), names[target.provider])()
    return result


def upload(upload_plan: UploadPlan, jobs_dir=None, *, publishers_factory=publishers,
           on_done: Callable[[str, PlatformRecord], None] | None = None,
           on_created: Callable[[Job], None] | None = None,
           on_start: Callable[[str], None] | None = None) -> Job:
    """Create and approve a job after the caller has obtained user approval."""
    job = pipeline.create_job(upload_plan.video, upload_plan.targets, jobs_dir)
    pipeline.approve(job, jobs_dir)
    if on_created:
        on_created(job)
    with pipeline.job_lock(job.id, jobs_dir):
        if on_start is None:
            pipeline.run(job, publishers_factory(upload_plan.targets), jobs_dir, on_done=on_done)
        else:
            pipeline.run(job, publishers_factory(upload_plan.targets), jobs_dir, on_done=on_done, on_start=on_start)
    return job


def status(job_id, jobs_dir=None, *, publishers_factory=publishers):
    with pipeline.job_lock(job_id, jobs_dir):
        job = pipeline.load_job(job_id, jobs_dir)
        return pipeline.refresh_status(job, publishers_factory(job.targets), jobs_dir)


def retry(job_id, jobs_dir=None, *, publishers_factory=publishers):
    with pipeline.job_lock(job_id, jobs_dir):
        job = pipeline.load_job(job_id, jobs_dir)
        return pipeline.retry(job, publishers_factory(job.targets), jobs_dir)


def history(jobs_dir=None):
    directory = Path(jobs_dir or pipeline.default_jobs_dir())
    return [pipeline.load_job(path.stem, directory) for path in sorted(directory.glob("*.json"))]


def setup_steps(platform):
    from .setup import STEPS
    return STEPS[platform]()


def submit_step(platform, step_id, value, *, store=None):
    from .setup import Progress
    progress = Progress(store or TokenStore())
    step = next((s for s in setup_steps(platform) if s.id == step_id), None)
    if step is None:
        raise ValueError("알 수 없는 연결 단계입니다")
    if step.is_field:
        updates, error = step.validate(value, progress.creds(platform))
        if error is not None or updates is None:
            return error
        progress.save_creds(platform, updates)
    progress.mark(platform, step_id)
    return None


def connect(platform, *, store=None, auth_factory=AuthManager, **kwargs):
    return auth_factory(platform, store=store or TokenStore(), **kwargs).authorize()
