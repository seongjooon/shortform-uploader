"""네트워크 없는 테스트용 Publisher. 시나리오를 주입해 각 상태 전이를 재현한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import (
    DefinitiveRejection,
    PublishResult,
    ReconcileStatus,
    ReconcileResult,
    ServerError,
    TimeoutAfterAccept,
    TransportError,
)

OK = "ok"
FAIL = "fail"  # 수락 전 확정 거부
ACCEPT_THEN_TIMEOUT = "accept_then_timeout"  # remote_id 발급 후 타임아웃
TRANSPORT_TIMEOUT = "transport_timeout"  # remote_id 없이 전송 중 타임아웃
SERVER_ERROR = "server_error"  # 일반 5xx
UNEXPECTED_ERROR = "unexpected_error"  # 분류되지 않은 예외
CRASH_AFTER_SEND = "crash_after_send"  # 전송 직후·결과 저장 전 프로세스 크래시
SCENARIOS = frozenset(
    {OK, FAIL, ACCEPT_THEN_TIMEOUT, TRANSPORT_TIMEOUT, SERVER_ERROR, UNEXPECTED_ERROR, CRASH_AFTER_SEND}
)


class SimulatedCrash(BaseException):
    """프로세스 강제 종료 시뮬레이션. `except Exception`에 잡히지 않는다."""


@dataclass
class PublishCall:
    video_path: str
    target: dict[str, Any]
    request_key: str | None


@dataclass
class MockPublisher:
    """scenarios는 publish 호출마다 순서대로 소비되고, 마지막 값은 이후 계속 재사용된다."""

    name: str
    scenarios: str | list[str] = OK
    reconcile_result: ReconcileStatus | ReconcileResult = "published"
    supports_idempotency_key: bool = False
    not_found_is_definitive: bool = False
    calls: list[PublishCall] = field(default_factory=list)
    reconcile_calls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.scenarios, str):
            self.scenarios = [self.scenarios]
        unknown = set(self.scenarios) - SCENARIOS
        if not self.scenarios or unknown:
            raise ValueError(f"알 수 없는 시나리오: {sorted(unknown)}")

    @property
    def publish_count(self) -> int:
        return len(self.calls)

    def _next_scenario(self) -> str:
        index = min(len(self.calls), len(self.scenarios)) - 1
        return self.scenarios[index]

    def publish(
        self,
        video_path: str,
        target: dict[str, Any],
        request_key: str | None = None,
        on_handle=None,
    ) -> PublishResult:
        self.calls.append(PublishCall(video_path, target, request_key))
        scenario = self._next_scenario()
        remote_id = f"{self.name}-{len(self.calls)}"

        if scenario == OK:
            return PublishResult(remote_id=remote_id, url=f"https://mock.invalid/{self.name}/{remote_id}")
        if scenario == FAIL:
            raise DefinitiveRejection(f"{self.name}: 유효성 오류(mock)")
        if scenario == ACCEPT_THEN_TIMEOUT:
            raise TimeoutAfterAccept(remote_id)
        if scenario == TRANSPORT_TIMEOUT:
            raise TransportError(f"{self.name}: 전송 중 타임아웃(mock)")
        if scenario == SERVER_ERROR:
            raise ServerError(503)
        if scenario == UNEXPECTED_ERROR:
            raise RuntimeError(f"{self.name}: 예상치 못한 오류(mock)")
        raise SimulatedCrash(f"{self.name}: 전송 직후 크래시(mock)")

    def reconcile(self, remote_id: str) -> ReconcileResult:
        self.reconcile_calls.append(remote_id)
        return ReconcileResult(self.reconcile_result) if isinstance(self.reconcile_result, str) else self.reconcile_result
