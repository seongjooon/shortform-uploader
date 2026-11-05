"""게시 파이프라인: 생성 → 승인 → 실행 → 복구/재시도 (SPEC §2).

job은 매 상태 변경 직후 jobs/<id>.json에 원자적으로 저장된다.
특히 publish 호출 '전에' publishing 상태를 디스크에 남겨, 결과 저장 전에 프로세스가
죽어도 재시작 시 그 레코드를 unknown으로 복구할 수 있게 한다.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
import re
import fcntl
from contextlib import contextmanager
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from . import approval as _approval
from .approval import approval_valid
from .models import (
    FAILED,
    PUBLISHED,
    PUBLISHING,
    QUEUED,
    UNKNOWN,
    Job,
    PlatformRecord,
    PlatformTarget,
)
from .publishers.base import DefinitiveRejection, Publisher, ReconcileResult, TimeoutAfterAccept

__all__ = [
    "approval_valid",
    "approve",
    "create_job",
    "job_path",
    "load_job",
    "recover",
    "retry",
    "run",
    "save_job",
]

APPROVAL_REQUIRED = "승인 필요 또는 승인 후 변경됨"


def default_jobs_dir() -> Path:
    return Path(os.environ.get("SHORTFORM_JOBS_DIR", "jobs"))


def job_path(job_id: str, jobs_dir: str | Path | None = None) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", job_id):
        raise ValueError("잘못된 job ID")
    return Path(jobs_dir or default_jobs_dir()) / f"{job_id}.json"


def save_job(job: Job, jobs_dir: str | Path | None = None) -> Path:
    """temp 파일에 쓰고 fsync 후 rename — 중간에 죽어도 반쯤 쓰인 json이 남지 않는다."""
    path = job_path(job.id, jobs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{job.id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(job.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def load_job(job_id: str, jobs_dir: str | Path | None = None) -> Job:
    with open(job_path(job_id, jobs_dir), encoding="utf-8") as f:
        return Job.from_dict(json.load(f))


def create_job(
    video_path: str | Path,
    targets: Mapping[str, PlatformTarget],
    jobs_dir: str | Path | None = None,
) -> Job:
    video = Path(video_path)
    if not video.is_file():
        raise ValueError(f"영상 파일이 없습니다: {video}")
    if not targets:
        raise ValueError("게시 대상 플랫폼이 없습니다")
    job = Job(
        id=uuid.uuid4().hex[:12],
        video_path=str(video.resolve()),
        targets=dict(targets),
        records={p: PlatformRecord(platform=p, state=QUEUED) for p in targets},
        approval=None,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    save_job(job, jobs_dir)
    return job


def approve(job: Job, jobs_dir: str | Path | None = None) -> Job:
    """현재 영상/targets 스냅샷에 대한 승인을 기록하고 저장한다."""
    _approval.approve(job)
    save_job(job, jobs_dir)
    return job


def recover(job: Job, jobs_dir: str | Path | None = None) -> Job:
    """재시작 시 publishing 레코드는 결과를 알 수 없으므로 unknown으로 전환한다(재큐 금지)."""
    changed = False
    for rec in job.records.values():
        if rec.state == PUBLISHING:
            rec.state = UNKNOWN
            rec.error = "publishing 중 프로세스 종료 — 원격 결과 미확인"
            if rec.remote_id is None and rec.handle is None:
                rec.needs_manual = True
            changed = True
    if changed:
        save_job(job, jobs_dir)
    return job


def _require_approval(job: Job) -> None:
    if not approval_valid(job):
        raise RuntimeError(APPROVAL_REQUIRED)


def _attempt(
    job: Job,
    platform: str,
    publisher: Publisher,
    jobs_dir: str | Path | None,
) -> None:
    rec = job.records[platform]
    target = job.targets[platform]
    idempotent = bool(getattr(publisher, "supports_idempotency_key", False))

    rec.state = PUBLISHING
    rec.attempts += 1
    rec.remote_id = None
    rec.url = None
    rec.handle = None
    rec.delivery = None
    rec.note = None
    rec.error = None
    rec.needs_manual = False
    if idempotent and rec.request_key is None:
        rec.request_key = uuid.uuid4().hex
    # 전송 전에 publishing을 기록해 둔다. 이후 크래시 → recover에서 unknown.
    save_job(job, jobs_dir)

    def on_handle(handle):
        rec.handle = handle
        save_job(job, jobs_dir)

    try:
        result = publisher.publish(
            job.video_path,
            {"account_id": target.account_id, "privacy": target.privacy, "metadata": dict(target.metadata)},
            request_key=rec.request_key if idempotent else None,
            on_handle=on_handle,
        )
    except DefinitiveRejection as e:
        rec.state = FAILED
        rec.error = str(e)
    except TimeoutAfterAccept as e:
        rec.state = UNKNOWN
        rec.remote_id = e.remote_id
        rec.error = str(e)
        rec.needs_manual = e.remote_id is None and rec.handle is None
    except Exception as e:  # 5xx/전송 오류 등: 서버 도달 여부를 모르므로 failed가 아니다
        rec.state = UNKNOWN
        rec.error = f"{type(e).__name__}: {e}"
        rec.needs_manual = rec.remote_id is None and rec.handle is None
    else:
        rec.state = PUBLISHED
        rec.remote_id = result.remote_id
        rec.url = result.url
        rec.delivery = result.delivery
        rec.note = result.note
    save_job(job, jobs_dir)


def _mark_missing_publisher(job: Job, platform: str, jobs_dir: str | Path | None) -> None:
    # 아무것도 전송하지 않았으므로 확정 실패로 기록해도 중복 게시 위험이 없다.
    rec = job.records[platform]
    rec.state = FAILED
    rec.error = f"publisher 없음: {platform}"
    save_job(job, jobs_dir)


def run(
    job: Job,
    publishers: Mapping[str, Publisher],
    jobs_dir: str | Path | None = None,
    on_done: Callable[[str, PlatformRecord], None] | None = None,
) -> Job:
    """queued 레코드만 게시한다. published/failed/unknown은 건드리지 않는다."""
    _require_approval(job)
    recover(job, jobs_dir)
    for platform, rec in job.records.items():
        if rec.state != QUEUED:
            continue
        publisher = publishers.get(platform)
        if publisher is None:
            _mark_missing_publisher(job, platform, jobs_dir)
        else:
            _attempt(job, platform, publisher, jobs_dir)
        if on_done is not None:
            on_done(platform, job.records[platform])
    return job


def _can_resubmit_after_not_found(publisher: Publisher) -> bool:
    return bool(
        getattr(publisher, "not_found_is_definitive", False)
        or getattr(publisher, "supports_idempotency_key", False)
    )


def retry(
    job: Job,
    publishers: Mapping[str, Publisher],
    jobs_dir: str | Path | None = None,
) -> Job:
    """failed는 즉시 재시도, unknown은 reconcile 우선. remote_id 없는 unknown은 재제출하지 않는다."""
    _require_approval(job)
    recover(job, jobs_dir)
    for platform, rec in job.records.items():
        if rec.state not in (FAILED, UNKNOWN):
            continue
        publisher = publishers.get(platform)
        if publisher is None:
            if rec.state == UNKNOWN:
                rec.needs_manual = True
                save_job(job, jobs_dir)
            continue

        if rec.state == FAILED:
            if rec.needs_manual:
                continue
            _attempt(job, platform, publisher, jobs_dir)
            continue

        if rec.remote_id is None and rec.handle is None:
            rec.needs_manual = True
            save_job(job, jobs_dir)
            continue

        try:
            result = publisher.reconcile(rec.handle or rec.remote_id)
            if isinstance(result, str):  # Backward-compatible mock scenarios
                result = ReconcileResult(result)
            status = result.status
        except Exception as e:
            rec.error = f"reconcile 실패: {type(e).__name__}: {e}"
            save_job(job, jobs_dir)
            continue

        if status == "published":
            rec.state = PUBLISHED
            rec.error = None
            rec.needs_manual = False
            rec.remote_id = result.remote_id or rec.remote_id
            rec.url = result.url or rec.url
            rec.delivery = result.delivery or rec.delivery
        elif status == "rejected":
            rec.state = FAILED
            rec.needs_manual = result.needs_manual
            rec.error = "원격 처리 거부 확정"
        elif status == "not_found" and _can_resubmit_after_not_found(publisher):
            _attempt(job, platform, publisher, jobs_dir)
            continue
        elif status == "not_found":
            rec.needs_manual = True
            rec.error = "reconcile=not_found — 반영 지연 가능성, 제공자 보장 없음 → 수동 확인 필요"
        if result.needs_manual:
            rec.needs_manual = True
        # status == "unknown": 상태 유지(대기)
        save_job(job, jobs_dir)
    return job


def refresh_status(job, publishers, jobs_dir=None):
    """Observe unresolved uploads and TikTok inbox delivery; never submit content."""
    recover(job, jobs_dir)
    for platform, rec in job.records.items():
        if rec.state != UNKNOWN and not (rec.state == PUBLISHED and rec.delivery == "inbox"):
            continue
        identifier = rec.handle or rec.remote_id
        publisher = publishers.get(platform)
        if not identifier or publisher is None:
            continue
        try:
            result = publisher.reconcile(identifier)
            if isinstance(result, str):
                result = ReconcileResult(result)
        except Exception:
            rec.error = "원격 상태 조회 실패 — 인증과 네트워크를 확인하세요"
        else:
            if result.status == "published":
                rec.state = PUBLISHED
                rec.remote_id = result.remote_id or rec.remote_id
                rec.url = result.url or rec.url
                rec.delivery = result.delivery or rec.delivery
                rec.error = None
                rec.needs_manual = False
            elif result.status == "rejected":
                rec.state = FAILED
                rec.error = "원격 처리 거부 확정"
                rec.needs_manual = result.needs_manual
            elif result.status == "not_found":
                rec.needs_manual = True
            if result.needs_manual:
                rec.needs_manual = True
        save_job(job, jobs_dir)
    return job


@contextmanager
def job_lock(job_id, jobs_dir=None):
    """Serialize CLI status/retry read-modify-write across processes."""
    path = job_path(job_id, jobs_dir).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("이 job은 다른 프로세스가 처리 중입니다") from None
        yield
    finally:
        os.close(fd)
