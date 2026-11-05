# 숏폼 파이프라인 스펙

> 초기 설계 참고 문서입니다. 현재 사용자 확정 요구와 구현 차이는 `docs/implementation.md`,
> 실행·인증 방법은 `README.md`를 기준으로 확인하세요. TikTok은 inbox 전달 후 사용자가 최종 게시합니다.
> 실제 플랫폼 스모크 테스트는 아직 수행하지 않았습니다.


## 1. 범위
- 입력: 완성된 MP4(세로 1080x1920 권장). 영상 생성은 이번 범위에서 제외.
- 게시: 플랫폼별 메타데이터(제목/캡션/설명/공개설정) → 승인 → 게시 → 결과 기록.
- 제외(1차): 편집/자막, 스케줄링 서버, 다계정 병렬.

## 2. 상태 모델 (핵심)
플랫폼별 게시 상태:
`queued → publishing → published | failed | unknown`
- **failed는 '수락 전 확정 거부'만**: 플랫폼이 게시 요청을 받기 전에 명확히
  거부한 경우(유효성 오류, 권한 없음 등)만 failed. 재시도 가능.
- **unknown은 '결과를 모르는 모든 경우'**:
  (a) remote_id를 받은 뒤 타임아웃/연결 끊김,
  (b) remote_id를 받기 **전** 전송 중 타임아웃/5xx/프로세스 크래시 — 요청이
      서버에 도달했을 수 있으므로 절대 실패로 간주하지 않음,
  (c) 재시작 시 디스크에서 publishing 상태로 복구된 레코드 — 복구 즉시
      unknown으로 전환, **블라인드 재큐 금지**.
- **unknown의 재시도 규칙**:
  1. remote_id가 있으면 `publisher.reconcile(remote_id)`로 원격 확인.
     `published`면 성공 처리, `unknown`이면 대기.
  2. reconcile이 `not_found`여도 이벤추얼 컨시스턴시(반영 지연)일 수 있으므로,
     제공자가 '조회 불가=미존재 확정' 또는 멱등성 키를 문서로 보장할 때만
     자동 재제출. 그렇지 않으면 사람이 확인하는 수동 정리(needs_manual)로 표시.
  3. remote_id가 없으면 원격 조회 자체가 불가하므로 needs_manual.
- **요청 키**: 제공자가 멱등성/요청 키(예: Idempotency-Key)를 지원하면
  타겟별로 생성해 기록·재사용(재제출 시 같은 키).
- 프로세스가 죽어도 job이 디스크에 남으므로, 재시작 후 published 플랫폼은
  절대 다시 실행하지 않음(중복 게시 방지).
- 한 플랫폼의 예외가 다른 플랫폼의 실행/기록을 중단시키지 않음
  (플랫폼마다 try/except로 감싸고 결과를 전부 기록).

## 3. 승인 바인딩 (사람 승인의 대상을 정확히 고정)
승인은 아래 스냅샷에 대해서만 유효:
- 영상 파일의 SHA-256
- 플랫폼별 {provider, account_id, privacy, metadata(제목/캡션/설명)} 전체
하나라도 바뀌면 승인은 자동 무효화되고 run은 거부.
"이 영상을, 이 문구로, 이 계정에, 이 공개설정으로 올린다"에 대한 승인이어야 함.

## 4. Publisher 인터페이스
```python
class Publisher(Protocol):
    name: str
    def publish(self, video_path, target) -> PublishResult: ...
        # target = {account_id, privacy, metadata}; 반환에 remote_id 포함 가능
    def reconcile(self, remote_id) -> str: ...  # "published" | "not_found" | "unknown"
```
- 1차 구현은 MockPublisher만. 네트워크 호출은 라이브 어댑터 안에만 존재.
- MockPublisher는 테스트용으로 시나리오 주입 가능(성공/실패/접수 후 타임아웃).

## 5. 플랫폼 경계 (2026-10-31 공식 문서 조사)
- **YouTube**: Data API v3로 본인 채널 게시 가능. 기본 할당량 100회/일.
  3분 이하 세로는 Shorts 자동 분류. OAuth Testing 모드는 토큰 7일 만료.
  https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits
  https://support.google.com/youtube/answer/10059070
- **Instagram**: 비즈니스/크리에이터 계정 필요. 컨테이너 2단계 게시.
  영상은 공개 URL에서만 가져감. 계정당 100개/24시간.
  본인 앱 역할 계정의 심사 면제 조항은 Meta 문서 접근 불가로 미검증.
  https://www.postman.com/meta/instagram/folder/23987686-bc459e67-42aa-4ea0-ad25-e5a6e42c3a83
- **TikTok**: 본인 계정 업로드 유틸리티는 Direct Post 감사 대상 용도에서 명시 제외.
  미감사 Direct Post는 SELF_ONLY만. 초안(draft) 경로의 개인용 적격 여부는 문서에
  명시 없음(미확인 — 확정된 선택지로 제시하지 않음).
  통합 게시 API(예: Upload-Post, 문서상 post_mode=DIRECT_POST 기본·privacy_level 지원)를
  Publisher 어댑터로 붙이는 경로도 있음(유료, 선택 사항).
  https://developers.tiktok.com/doc/content-sharing-guidelines
  https://docs.upload-post.com/api/upload-video

## 6. 미결정 사항 (mock 작업과 무관, 라이브 전에 정하면 됨)
- TikTok 처리 방식 / Instagram 계정 유형 / 콘텐츠 소스(Runmate 여부) /
  라이브 어댑터로 공식 API vs 통합 API 중 무엇을 쓸지
