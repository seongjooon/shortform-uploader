# PRD — 숏폼 3개 플랫폼 동시 업로드

> 초기 설계 참고 문서입니다. 현재 사용자 확정 요구와 구현 차이는 `docs/implementation.md`,
> 실행·인증 방법은 `README.md`를 기준으로 확인하세요. TikTok은 inbox 전달 후 사용자가 최종 게시합니다.
> 실제 플랫폼 스모크 테스트는 아직 수행하지 않았습니다.


작성일: 2026-10-31 · 상태: 확정(라이브 어댑터 착수용)

## 1. 문제와 목표
완성된 세로 숏폼 MP4 하나를 YouTube Shorts, Instagram Reels, TikTok에 올릴 때
플랫폼마다 앱을 열고 같은 문구를 반복 입력해야 한다.
**명령 한 번 + 사람 승인 한 번**으로 세 플랫폼 게시를 끝내고,
결과(성공/실패/확인 필요)를 한 곳에서 본다.

## 2. 사용자
본인 1명. 본인 소유 계정 3개(YouTube 채널 1, Instagram 프로페셔널 계정 1, TikTok 1)만 다룬다.

## 3. 확정된 결정 (2026-10-31)
| 항목 | 결정 | 이유 |
|---|---|---|
| 실행 형태 | 로컬 CLI (`python -m shortform ...`) | 서버·스케줄러 불필요, 토큰이 로컬에만 존재 |
| YouTube | Data API v3 `videos.insert` resumable 업로드로 직접 게시 | 공식 경로로 본인 채널 게시 가능 |
| Instagram | 프로페셔널 계정 + Facebook 페이지 연결, **Facebook Login for Business + resumable 업로드** | 로컬 파일을 바로 올릴 수 있어 공개 호스팅 불필요 (resumable은 FB Login for Business 전용) |
| TikTok | **초안(inbox) 업로드** — API로 받은편지함까지 보내고 앱에서 사용자가 직접 게시 | 미감사 Direct Post는 SELF_ONLY + 비공개 계정만 가능하고, "본인/팀 계정용 업로드 도구"는 감사 허용 대상이 아님 |
| 통합 API(Upload-Post 등) | 사용하지 않음 | 비용·토큰 위탁 회피. 필요 시 Publisher 어댑터로 추가 가능 |

## 4. 범위
### 포함
- 플랫폼별 메타데이터(제목/캡션/설명/공개설정) 설정 파일(`shortform.toml`)
- 승인 바인딩(영상 해시 + 타깃 전체) — Task 1 구현 유지
- 3개 플랫폼 라이브 Publisher 어댑터 + reconcile
- OAuth 최초 인증(`shortform auth <platform>`), 토큰 로컬 저장·갱신
- 사전 검증(파일 형식/크기/길이, 플랫폼별 한도 사전 확인)
- `status`, `retry` CLI 명령

### 제외
- 영상 생성·편집·자막, 예약 게시, 서버, 다계정
- TikTok 완전 자동 공개 게시(감사 정책상 불가)
- 웹 UI(FE 없음 → `fe-plan.md` 작성하지 않음)

## 5. 사용자 흐름
1. `shortform auth youtube|instagram|tiktok` — 최초 1회 브라우저 로그인
2. `shortform run clip.mp4 --config shortform.toml --dry-run` — 계획 확인
3. `shortform run clip.mp4 --config shortform.toml` — 계획 출력 → `y` 승인 → 게시
4. 결과:
   - YouTube/Instagram: `published` + URL
   - TikTok: `published (delivery=inbox)` + "TikTok 앱 받은편지함에서 게시하세요" + 붙여넣을 캡션 출력
5. `shortform status <job>` / `shortform retry <job>` — unknown/failed 처리

## 6. 성공 기준
- 비공개(private/SELF_ONLY 등) 설정으로 3개 플랫폼 스모크 테스트 각 1회 성공
- 어떤 장애 상황에서도 **중복 게시 0건**(SPEC §2 상태 규칙 유지)
- 단위 테스트는 네트워크 0(소켓 차단 유지), 라이브 테스트는 opt-in

## 7. 플랫폼 제약 (공식 문서 근거)
### YouTube
- `videos.insert`: Video Uploads 버킷 1회/호출, 일 100회. 스코프 `youtube.upload` 등.
  `privacyStatus` 미지정 시 기본 `public` → **항상 명시**.
  문서 현재 문구: "Videos uploaded from unverified API projects are not restricted to private viewing mode."
  (과거 정책과 다르므로 스모크 테스트로 재확인)
  https://developers.google.com/youtube/v3/docs/videos/insert
- Resumable: 세션 URI 발급 → 중단 시 `PUT Content-Range: bytes */N`으로 상태 조회
  (308=진행 중, 201=완료+video 리소스, 404=세션 만료).
  https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
- Shorts 분류(3분 이하 세로/정사각): https://support.google.com/youtube/answer/10059070
- OAuth Testing 모드 토큰 7일 만료(SPEC §5)

### Instagram
- 프로페셔널 계정 필수. 페이지가 PPA 대상이면 완료 전 게시 차단.
- 권한(FB Login for Business): `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`
  (+ 페이지에 Business Manager 역할이 있으면 `ads_management`, `ads_read`).
- 흐름: `POST /<IG_ID>/media`(media_type=REELS, upload_type=resumable) →
  `POST rupload.facebook.com/ig-api-upload/<ver>/<container_id>` →
  `GET /<container_id>?fields=status_code` → `POST /<IG_ID>/media_publish`.
- 컨테이너 상태: IN_PROGRESS / FINISHED / PUBLISHED / ERROR / EXPIRED(24시간 미게시).
  상태 확인은 1분 간격, 최대 5분 권장.
- 한도: 24시간 롤링 100건(같은 문서 안에 50건 표기도 있음 → `content_publishing_limit`으로 실측).
  https://developers.facebook.com/docs/instagram-platform/content-publishing

### TikTok
- 미감사 Direct Post: SELF_ONLY만, 계정 비공개 필수, 24시간 5명.
  "본인/팀 관리 계정용 업로드 도구"는 허용되지 않는 용도로 명시.
  https://developers.tiktok.com/doc/content-sharing-guidelines
- Inbox 업로드: `POST /v2/post/publish/inbox/video/init/`, 스코프 `video.upload`
  (**앱이 해당 스코프 승인을 받아야 함**). 사용자가 앱 알림을 눌러 편집·게시 완료.
  24시간 내 대기 중 업로드 최대 5건. 토큰당 6 req/min. upload_url 1시간 유효.
  https://developers.tiktok.com/doc/content-posting-api-reference-upload-video
  https://developers.tiktok.com/doc/content-posting-api-get-started-upload-content
- 청크: 5MB~64MB(마지막 최대 128MB), 1~1000개, 순차 업로드, 5MB 미만은 단일 청크.
  MP4/H.264 권장, 23~60fps, 가로·세로 360~4096px, 최대 4GB.
  https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
- 상태 조회: `POST /v2/post/publish/status/fetch/` (30 req/min).
  PROCESSING_UPLOAD → SEND_TO_USER_INBOX → (사용자 게시 후) PUBLISH_COMPLETE / FAILED.
  https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status

## 8. 리스크 / 미확인 사항
| # | 내용 | 대응 |
|---|---|---|
| R1 | TikTok 앱의 `video.upload` 스코프 승인이 개인용 앱에 나는지 문서상 불명 | Wave 0에서 신청. 반려 시 TikTok 어댑터 보류(YouTube/IG만 운영), 통합 API 재검토 |
| R2 | Meta 앱을 App Review 없이 본인(앱 역할) 계정에 쓸 수 있는지 미검증 | Wave 0에서 개발 모드로 실측 |
| R3 | Instagram 장기 토큰 수명·갱신 방식 미검증 | Wave 4 착수 전 공식 문서 확인 후 be-plan 갱신 |
| R4 | TikTok 데스크톱 OAuth의 redirect URI 제약(localhost 허용 여부) 미확인 | Wave 0에서 앱 설정 화면으로 확인 |
| R5 | YouTube 미검증 프로젝트 업로드의 공개 제한 여부(문서 문구가 과거와 다름) | 스모크 테스트에서 private→unlisted로 확인 |
| R6 | IG `media_publish`를 같은 FINISHED 컨테이너로 재호출해도 중복이 안 생기는지 문서 보장 없음 | 보장 전까지 `not_found_is_definitive=False` 유지(=needs_manual) |
