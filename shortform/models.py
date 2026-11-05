"""Job / 플랫폼 레코드 / 승인 데이터 모델 (SPEC §2, §3)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

QUEUED = "queued"
PUBLISHING = "publishing"
PUBLISHED = "published"
FAILED = "failed"
UNKNOWN = "unknown"
STATES = frozenset({QUEUED, PUBLISHING, PUBLISHED, FAILED, UNKNOWN})


@dataclass
class PlatformTarget:
    provider: str
    account_id: str
    privacy: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformTarget:
        return cls(
            provider=data["provider"],
            account_id=data["account_id"],
            privacy=data["privacy"],
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class PlatformRecord:
    platform: str
    state: str
    remote_id: str | None = None
    url: str | None = None
    error: str | None = None
    attempts: int = 0
    request_key: str | None = None
    needs_manual: bool = False
    handle: str | None = None
    delivery: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"알 수 없는 상태: {self.state}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformRecord:
        return cls(**data)


@dataclass
class Approval:
    video_sha256: str
    targets_hash: str
    approved_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Approval:
        return cls(**data)


@dataclass
class Job:
    id: str
    video_path: str
    targets: dict[str, PlatformTarget]
    records: dict[str, PlatformRecord]
    approval: Approval | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "video_path": self.video_path,
            "targets": {p: t.to_dict() for p, t in self.targets.items()},
            "records": {p: r.to_dict() for p, r in self.records.items()},
            "approval": self.approval.to_dict() if self.approval else None,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        approval = data.get("approval")
        return cls(
            id=data["id"],
            video_path=data["video_path"],
            targets={p: PlatformTarget.from_dict(t) for p, t in data["targets"].items()},
            records={p: PlatformRecord.from_dict(r) for p, r in data["records"].items()},
            approval=Approval.from_dict(approval) if approval else None,
            created_at=data["created_at"],
        )
