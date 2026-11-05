"""승인 바인딩 (SPEC §3).

승인은 '이 영상(SHA-256)을, 이 문구로, 이 계정에, 이 공개설정으로'에 대해서만 유효하다.
영상 파일이나 targets가 1바이트라도 바뀌면 approval_valid는 False.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .models import Approval, Job, PlatformTarget

_CHUNK = 1024 * 1024


def video_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def targets_hash(targets: dict[str, PlatformTarget]) -> str:
    # 키 정렬 + 고정 구분자로 정규화해 dict 순서와 무관하게 같은 해시가 나오게 한다.
    canonical = json.dumps(
        {platform: target.to_dict() for platform, target in targets.items()},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approve(job: Job) -> Approval:
    job.approval = Approval(
        video_sha256=video_sha256(job.video_path),
        targets_hash=targets_hash(job.targets),
        approved_at=datetime.now(timezone.utc).isoformat(),
    )
    return job.approval


def approval_valid(job: Job) -> bool:
    if job.approval is None:
        return False
    try:
        current_video = video_sha256(job.video_path)
    except OSError:
        return False
    return (
        current_video == job.approval.video_sha256
        and targets_hash(job.targets) == job.approval.targets_hash
    )
