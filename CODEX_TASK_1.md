# Codex 작업 1: 뼈대 + MockPublisher (네트워크 없음)

SPEC.md를 먼저 읽어. 실제 API 호출/계정/유료 서비스 금지. Python 3.11+, 의존성 pytest만.

## 만들 것
1. `shortform/models.py`
   - `PlatformTarget(provider, account_id, privacy, metadata: dict)`
   - `PlatformRecord(platform, state, remote_id=None, url=None, error=None, attempts=0,
     request_key=None, needs_manual=False)`
     state ∈ {queued, publishing, published, failed, unknown}
   - `Job(id, video_path, targets: dict[platform, PlatformTarget],
     records: dict[platform, PlatformRecord], approval: Approval|None, created_at)`
   - `Approval(video_sha256, targets_hash, approved_at)`
2. 승인 바인딩 (SPEC §3)
   - `approve(job)`: 영상 SHA-256 + targets 정규화 해시를 계산해 Approval 저장
   - `approval_valid(job)`: 현재 영상/targets를 다시 해시해 비교
   - 영상 파일 또는 targets가 1바이트라도 바뀌면 무효
3. `shortform/publishers/base.py` — SPEC §4의 Publisher 프로토콜
   (`publish`는 remote_id를 담은 PublishResult 반환 가능, `reconcile(remote_id)` 필수)
4. `shortform/publishers/mock.py`
   - 시나리오 주입 가능: ok / fail / accept_then_timeout(remote_id 발급 후 타임아웃 시뮬레이션)
   - reconcile은 주입된 시나리오에 따라 published/not_found/unknown 반환
5. `shortform/pipeline.py`
   - `create_job` (영상 없으면 ValueError, records를 queued로 초기화)
   - `approve`, `approval_valid`
   - `run(job, publishers)`: 승인 무효하면 RuntimeError("승인 필요 또는 승인 후 변경됨").
     플랫폼별로 개별 try/except — 한 플랫폼 예외가 다른 플랫폼 실행·기록을 막지 않음.
     상태 전이는 SPEC §2 규칙대로:
     - 수락 전 확정 거부(DefinitiveRejection)만 failed
     - 원격 접수 후 타임아웃(TimeoutAfterAccept, remote_id 있음) → unknown + remote_id
     - remote_id 없는 전송 중 타임아웃/일반 5xx/전송 오류 → unknown(remote_id=None) + needs_manual
     - 이미 published인 플랫폼은 건너뜀
     - provider가 멱등성 키를 지원하면 타겟별 request_key 생성·기록·재사용
   - `recover(job)`: 재시작 시 publishing 상태 레코드를 전부 unknown으로 전환(재큐 금지)
   - `retry(job, publishers)`: failed만 즉시 재시도. unknown은:
     - remote_id 있음 → reconcile 먼저: published→성공 처리, unknown→유지,
       not_found→제공자의 미존재 확정/멱등성 보장이 문서화된 경우에만 재제출,
       아니면 needs_manual=True로 표시하고 재제출하지 않음
     - remote_id 없음 → needs_manual=True (자동 재제출 금지)
   - job은 매 단계 후 `jobs/<id>.json`에 원자적으로 저장(temp 파일 + rename)
6. CLI `shortform/cli.py`
   - `python -m shortform run <video> --title T --platforms mockA,mockB --dry-run`
   - --dry-run: 검증과 계획 출력만, approve/run 호출 없음
7. 테스트 `tests/`

## 인수 테스트 (pytest 전부 통과해야 완료)
- [ ] dry-run은 publisher를 한 번도 호출하지 않고 종료코드 0
- [ ] 없는 영상 경로 create_job → ValueError
- [ ] approve 없이 run → RuntimeError
- [ ] approve 후 영상 내용을 1바이트 바꾸면 approval_valid=False, run 거부
- [ ] approve 후 targets의 caption 하나만 바꿔도 run 거부
- [ ] 정상 흐름: 각 플랫폼 1회씩 호출, job json에 mock URL 기록, 상태 published
- [ ] 플랫폼A 실패/플랫폼B 성공이어도 둘 다 기록되고 A=failed, B=published
- [ ] accept_then_timeout 시나리오(remote_id 있음): state=unknown + remote_id 저장.
      retry에서 reconcile이 published를 반환하면 재제출 없이 published 처리
- [ ] 전송 중 타임아웃(remote_id 없음) 시나리오: state=unknown + needs_manual=True,
      retry가 자동 재제출하지 않음
- [ ] reconcile이 not_found이고 제공자 멱등성 보장이 없으면 재제출하지 않고 needs_manual
- [ ] reconcile이 not_found이고 멱등성 보장이 설정된 경우에만 재제출 → published 기록
- [ ] run 도중 '전송 직후·결과 저장 전 크래시' 시뮬레이션: 디스크에서 job 다시 로드 시
      publishing 레코드가 unknown으로 전환되고 블라인드 재큐되지 않음
- [ ] 재시작 후 published 플랫폼은 재호출되지 않음(호출 카운트로 검증)
- [ ] 일반 5xx/전송 예외는 failed가 아니라 unknown으로 기록
- [ ] 소켓 사용 전역 차단 테스트(mock 단계에서 네트워크 0)

## 완료 조건
pytest 전체 통과 + dry-run 출력 확인. "동작한다"고 주장할 때는 위 테스트 결과만 근거로.
