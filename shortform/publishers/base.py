"""Publisher 프로토콜과 결과/예외 타입 (SPEC §4).

예외 분류가 곧 상태 전이 규칙이다 (SPEC §2):
- DefinitiveRejection: 플랫폼이 게시 요청을 받기 전에 명확히 거부 → failed
- TimeoutAfterAccept: 원격 접수(remote_id 발급) 후 결과 미확인 → unknown + remote_id
- 그 밖의 모든 예외(전송 중 타임아웃, 5xx, 연결 오류 등) → unknown + needs_manual
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, runtime_checkable

ReconcileStatus = Literal["published", "not_found", "unknown", "rejected"]


@dataclass
class PublishResult:
    remote_id: str
    url: str | None = None
    delivery: str | None = None
    note: str | None = None


@dataclass
class ReconcileResult:
    status: ReconcileStatus
    remote_id: str | None = None
    url: str | None = None
    delivery: str | None = None
    needs_manual: bool = False


class PublishError(Exception):
    """Publisher가 던지는 예외의 기반 클래스."""


class DefinitiveRejection(PublishError):
    """수락 전 확정 거부(유효성 오류, 권한 없음 등). 재시도해도 중복 게시 위험 없음."""


class TimeoutAfterAccept(PublishError):
    """원격이 요청을 접수(remote_id 발급)한 뒤 결과를 받지 못함."""

    def __init__(self, remote_id: str | None, message: str = "접수 후 타임아웃") -> None:
        super().__init__(message)
        self.remote_id = remote_id


class TransportError(PublishError):
    """remote_id를 받기 전 전송 중 타임아웃/연결 끊김. 서버 도달 여부를 알 수 없음."""


class ServerError(PublishError):
    """일반 5xx. 서버가 처리했을 수도 있으므로 실패로 간주하지 않음."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status


@runtime_checkable
class Publisher(Protocol):
    name: str
    # 제공자가 멱등성/요청 키를 문서로 보장하는지. True면 request_key를 생성·재사용한다.
    supports_idempotency_key: bool
    # 제공자가 '조회 불가 = 미존재 확정'을 문서로 보장하는지.
    not_found_is_definitive: bool

    def publish(
        self,
        video_path: str,
        target: dict[str, Any],
        request_key: str | None = None,
        on_handle: Callable[[str], None] | None = None,
    ) -> PublishResult:
        """target = {account_id, privacy, metadata}."""
        ...

    def reconcile(self, remote_id: str) -> ReconcileResult: ...
