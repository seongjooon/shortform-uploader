# BE Plan — 라이브 Publisher 어댑터

> 초기 설계 참고 문서입니다. 현재 사용자 확정 요구와 구현 차이는 `docs/implementation.md`,
> 실행·인증 방법은 `README.md`를 기준으로 확인하세요. TikTok은 inbox 전달 후 사용자가 최종 게시합니다.
> 실제 플랫폼 스모크 테스트는 아직 수행하지 않았습니다.


근거 문서: `docs/prd.md`, `SPEC.md`. 완료된 작업: `CODEX_TASK_1.md`(뼈대 + MockPublisher, pytest 40개 통과).
FE 없음(CLI 전용) → `fe-plan.md` 없음.

## 공통 규칙 (모든 Wave에 적용 — Codex 지시 시 함께 전달)
- Python 3.11+. 런타임 의존성은 **표준 라이브러리만** 사용한다(`urllib.request`, `http.server`, `tomllib`, `json`, `secrets`, `hashlib`).
  테스트 의존성은 pytest만. Google/Meta SDK를 쓰지 않는다. 세 플랫폼 모두 REST로 직접 호출해, 테스트용 가짜 전송 계층을 똑같이 끼울 수 있게 한다.
- 네트워크 호출은 `shortform/http.py`의 `Transport` 하나로만 나간다. 어댑터는 Transport를 주입받는다.
- 단위 테스트에서는 `tests/conftest.py`의 소켓 전역 차단을 유지한다. 라이브 테스트는 `@pytest.mark.live`를 붙이고 `SHORTFORM_LIVE=1`일 때만 실행한다(기본은 skip).
- SPEC §2의 상태 규칙은 바꾸지 않는다. 결과를 모르면 unknown이다. **failed는 수락 전 확정 거부일 때만** 쓴다.
- 비밀값(client secret, 토큰)은 저장소에 두지 않는다. `~/.config/shortform/`에 두고 파일 권한은 0600으로 한다. 로그와 job json에 토큰을 출력하지 않는다.
- PR은 Wave마다 하나씩 연다(BE 전용). Wave 안에서도 Task 단위로 커밋하고, 한 커밋에 type을 섞지 않는다.
- PR 전에 QA 검증을 받는다. 통과한 뒤 PR을 연다. CI를 기다린 뒤 리뷰를 반영하고 머지 승인을 요청한다.

## Wave 개요
| Wave | 내용 | 선행 | 사람 작업 |
|---|---|---|---|
| 0 | 개발자 앱/계정 준비 (코딩 없음) | — | 필수 |
| 2 | 공통 인프라: 설정, Transport, 토큰 저장소, OAuth 루프백, 프로토콜 확장, CLI 확장 | Task 1 | — |
| 3 | YouTube 어댑터 | 2 | 스모크 1회 |
| 4 | Instagram 어댑터 | 2 | 스모크 1회 |
| 5 | TikTok inbox 어댑터 | 2 | 스모크 1회 |
| 6 | 통합 마무리: 사전 검증, 운영 문서, 3플랫폼 E2E 스모크 | 3, 4, 5 | 스모크 1회 |

Wave 3~5는 Wave 2가 머지된 뒤 병렬로 진행할 수 있다. 각 Wave의 PR은 따로 연다.

---

## Wave 0 — 사전 준비 (사람)
- [ ] **Google Cloud**: 프로젝트를 만들고 YouTube Data API v3를 활성화한다. OAuth 동의 화면은 External/Testing으로 두고 본인을 테스트 사용자로 추가한다. OAuth 클라이언트는 "Desktop app" 유형으로 만든다.
- [ ] **Meta**: Instagram을 프로페셔널 계정으로 전환한다. Facebook 페이지를 연결하고 PPA 필요 여부를 확인한다. Meta 앱(Business 유형)을 만들고 Facebook Login for Business를 추가한다. 본인을 앱 역할(관리자/테스터)에 등록한다(R2 실측).
- [ ] **TikTok**: developers.tiktok.com에서 앱을 만들고 Login Kit과 Content Posting API를 추가한다. `video.upload` 스코프 심사를 신청한다(R1). redirect URI 제약을 확인한다(R4).
- [ ] 결과를 `docs/prd.md` §8 리스크 표에 갱신한다.

---

## Wave 2 — 공통 인프라

### Task 2-1 설정 파일 `shortform.toml` (`shortform/config.py`)
```toml
[defaults]
title = "제목"
description = "설명"

[platforms.youtube]
enabled = true
account_id = "UCxxxx"          # 채널 ID
privacy = "private"            # private | unlisted | public
title = "Shorts 제목 #shorts"   # 생략 시 defaults
made_for_kids = false

[platforms.instagram]
enabled = true
account_id = "1784xxxx"        # IG user id
caption = "캡션 #reels"
share_to_feed = true

[platforms.tiktok]
enabled = true
account_id = "open_id"
caption = "앱에 붙여넣을 캡션"   # inbox 흐름에서는 API로 전송되지 않음(안내용)
```
- `load_config(path) -> dict[str, PlatformTarget]`: 플랫폼 metadata에 defaults를 병합한다. 알 수 없는 키나 privacy 값이 오면 ValueError를 낸다.
- 플랫폼별로 허용하는 privacy 값을 검증한다(YouTube: private/unlisted/public, Instagram: 해당 없음, TikTok: inbox라 해당 없음).

### Task 2-2 Transport (`shortform/http.py`)
- `Transport.request(method, url, headers, body: bytes | file-slice | None, timeout) -> Response(status, headers, body)`
- 예외는 두 종류로 나눈다.
  - `ConnectError`: 요청 바이트를 하나도 보내기 전에 실패(DNS, 연결 거부). 서버에 도달하지 않았다고 확정할 수 있다.
  - `TransportError`(기존 클래스 재사용): 송신을 시작한 뒤 타임아웃이나 연결이 끊긴 경우.
- `FakeTransport`(테스트용): 요청을 기록하고, 미리 지정한 응답이나 예외를 순서대로 돌려준다.

### Task 2-3 토큰 저장소 + OAuth 루프백 (`shortform/auth/`)
- `TokenStore`: `~/.config/shortform/tokens/<platform>.json`. 원자적으로 저장하고 권한은 0600으로 둔다. `SHORTFORM_CONFIG_DIR`로 경로를 바꿀 수 있다.
- `loopback_authorize(auth_url_builder, token_exchanger)`: `127.0.0.1:<임의포트>`에서 `http.server`로 코드를 받는다. `state`를 검증하고, 지원하는 플랫폼에는 PKCE(S256)를 적용한다.
- `refresh_if_needed(platform)`: 만료 60초 전이면 갱신한다. 갱신이 실패하면 `AuthRequired` 예외를 낸다. 이 예외는 publish 이전 단계에서 나므로 **DefinitiveRejection의 하위 클래스**다.

### Task 2-4 프로토콜 확장 (`publishers/base.py`, `models.py`, `pipeline.py`)
- `reconcile`의 반환값을 `ReconcileResult(status, remote_id=None, url=None, delivery=None)`로 바꾼다.
  `status`의 값은 `"published" | "not_found" | "unknown" | "rejected"`다.
  - `rejected`는 플랫폼이 "처리 실패, 게시되지 않음"을 확정했다는 뜻이다(예: TikTok `FAILED`, IG 컨테이너 `ERROR`). 레코드를 failed로 바꾸고, 다음 retry에서 재시도한다.
  - published일 때 remote_id/url이 오면 레코드를 갱신한다(YouTube 세션 URI → video id).
- `PlatformRecord`에 필드를 추가한다.
  - `delivery: str | None` — `"posted"` 또는 `"inbox"`.
  - `handle: str | None` — 업로드 도중에 쓰는 복구 핸들(YouTube 세션 URI, IG 컨테이너 id, TikTok publish_id). remote_id와 구분한다.
- 어댑터는 핸들을 받는 즉시 `on_handle(handle)` 콜백으로 파이프라인에 알린다. 파이프라인은 이 값을 **바로 디스크에 저장**한다. 그래야 크래시가 나도 recover 이후 reconcile을 할 수 있다.
  - 시그니처: `publish(video_path, target, request_key=None, on_handle=None)`
  - recover/retry 규칙을 확장한다. remote_id가 없더라도 handle이 있으면 `reconcile(handle)`이 가능하므로 needs_manual로 보내지 않는다.
- MockPublisher와 기존 테스트를 새 프로토콜에 맞게 고친다(기존 인수 테스트는 의미를 유지한 채 모두 통과해야 한다).

### Task 2-5 CLI 확장
- `shortform auth <platform>`
- `shortform run <video> --config shortform.toml [--only youtube,instagram] [--dry-run]`
- `shortform status <job_id>`: 레코드 표를 출력한다. TikTok inbox 레코드에는 안내 문구와 캡션을 함께 보여준다.
- `shortform retry <job_id>`
- 기존 `--title/--platforms` mock 모드는 유지한다(테스트용).

### Wave 2 인수 테스트
- [ ] toml을 로드하면 defaults가 병합되고, 잘못된 privacy 값은 ValueError를 낸다
- [ ] 토큰 파일 권한이 0600이고, 원자적 저장 후 임시 파일이 남지 않는다
- [ ] 루프백 OAuth에서 state가 다르면 거부한다(가짜 콜백 요청으로 검증하고 소켓은 쓰지 않는다. 핸들러 단위 테스트)
- [ ] on_handle로 받은 핸들이 publish 도중 크래시가 나도 디스크에 남는다. recover 후 retry에서 reconcile(handle)이 호출된다
- [ ] reconcile이 `rejected`를 주면 failed로 바뀌고, 다음 retry에서 재제출된다
- [ ] reconcile이 published와 함께 remote_id/url을 주면 레코드가 갱신된다
- [ ] Task 1의 인수 테스트 15개 항목이 전부 계속 통과한다

---

## Wave 3 — YouTube 어댑터 (`publishers/youtube.py`)

### 인증
- 스코프는 `https://www.googleapis.com/auth/youtube.upload`와 `https://www.googleapis.com/auth/youtube.readonly`(reconcile의 videos.list용)다.
- Desktop 클라이언트를 쓰고 루프백 redirect와 PKCE를 적용한다. Testing 모드에서는 refresh token이 7일 뒤 만료되므로, 만료되면 `AuthRequired`를 내고 `shortform auth youtube`를 안내한다.

### publish 흐름
1. 세션 시작: `POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status`
   - body: `snippet.title/description`, `status.privacyStatus`(**반드시 명시**, 미지정 시 public), `status.selfDeclaredMadeForKids`
   - 헤더: `X-Upload-Content-Length`, `X-Upload-Content-Type: video/mp4`
   - 응답 `Location`이 세션 URI다. 받는 즉시 `on_handle(session_uri)`를 호출한다.
   - 이 단계에서 4xx가 오면 DefinitiveRejection이다(아직 업로드 전). ConnectError도 DefinitiveRejection이다.
   - 이 단계의 5xx나 송신 후 타임아웃은 TransportError다(세션이 만들어졌는지 알 수 없다 → unknown + needs_manual).
2. 업로드: 세션 URI로 `PUT`. 중간에 끊기면 같은 세션 안에서 `Content-Range: bytes */N`으로 상태를 조회하고 이어 올린다(최대 N회, 지수 백오프).
   - 같은 세션 URI 안에서 재개하는 것은 새 영상을 만들지 않으므로 어댑터 내부에서 해도 된다.
3. `201` → `PublishResult(remote_id=video.id, url=f"https://www.youtube.com/shorts/{id}", delivery="posted")`
4. 재개 한도를 다 쓰면 `TimeoutAfterAccept(remote_id=None)`을 내고, 핸들은 이미 저장돼 있다.

### reconcile(handle_or_id)
- 세션 URI일 때는 `PUT Content-Range: bytes */N`을 보낸다.
  - 201/200 → published(+video id)
  - 308 → unknown. 재개는 retry에서 하지 않고 사람이 판단한다. 단 `resume` 옵션은 Wave 6에서 검토한다.
  - 404(세션 만료) → not_found
- video id일 때는 `videos.list?id=...&part=status`를 호출한다. 항목이 있으면 published, 없으면 not_found.
- `supports_idempotency_key = False`, `not_found_is_definitive = False`

### Wave 3 인수 테스트 (FakeTransport)
- [ ] 정상: 세션 → PUT 201 → published, URL 형식, privacyStatus가 요청 body에 들어 있음
- [ ] 세션 생성 400 → failed, PUT 호출 없음
- [ ] 업로드 중 끊김 → 308(Range) → 남은 바이트부터 재개 → 201. Content-Range 값 검증
- [ ] 재개 한도 초과 → unknown, handle=세션 URI 저장 → retry에서 reconcile이 201 → published(재업로드 없음)
- [ ] reconcile 404 → needs_manual, 재제출 없음
- [ ] 토큰 만료 → refresh 호출 → 성공 후 진행 / refresh 실패 → failed(AuthRequired), 업로드 호출 없음
- [ ] 라이브(opt-in): 3초짜리 세로 테스트 영상을 private로 업로드하고 `status`에 URL이 나오는지 확인

---

## Wave 4 — Instagram Reels 어댑터 (`publishers/instagram.py`)

### 인증
- Facebook Login for Business. 권한은 `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`(필요하면 `ads_management`, `ads_read`)다.
- 호스트는 `graph.facebook.com`과 `rupload.facebook.com`이다. 페이지 토큰을 쓴다.
- **착수 전 확인(R3)**: 장기 토큰의 수명과 갱신 방식을 공식 문서에서 확인한 뒤 이 섹션을 갱신한다. 확인 전에는 구현하지 않는다.

### publish 흐름
0. 사전 확인: `GET /<IG_ID>/content_publishing_limit`. 한도를 다 썼으면 DefinitiveRejection을 낸다.
1. 컨테이너 생성: `POST /<IG_ID>/media` (`media_type=REELS`, `upload_type=resumable`, `caption`, `share_to_feed`)
   → 컨테이너 id를 받으면 즉시 `on_handle(container_id)`를 호출한다.
   - 4xx → DefinitiveRejection. 5xx나 타임아웃 → TransportError(컨테이너 생성 여부를 모르지만, 컨테이너 자체는 게시가 아니므로 위험은 낮다. 그래도 SPEC 규칙대로 unknown 처리).
2. 업로드: `POST https://rupload.facebook.com/ig-api-upload/<ver>/<container_id>`, 헤더 `offset: 0`, `file_size`, body는 파일 바이트다.
3. 상태 폴링: `GET /<container_id>?fields=status_code`를 **1분 간격, 최대 5분** 돌린다.
   - `FINISHED` → 4단계로 간다. `ERROR` → DefinitiveRejection(게시되지 않았음이 확정). `IN_PROGRESS`가 5분 넘게 계속되면 TimeoutAfterAccept(container_id).
4. 게시: `POST /<IG_ID>/media_publish?creation_id=<container_id>` → media id
   - 타임아웃이나 5xx → `TimeoutAfterAccept(container_id)`
5. `GET /<media_id>?fields=permalink` → `PublishResult(remote_id=media_id, url=permalink, delivery="posted")`
- 폴링 간격은 주입 가능한 `sleep`으로 만들어 테스트에서 0으로 둔다.

### reconcile(container_id)
| status_code | 반환 |
|---|---|
| PUBLISHED | published (media id나 permalink 조회가 가능하면 채운다. 방법이 문서로 확인되지 않으면 url=None) |
| FINISHED | not_found (아직 게시되지 않음. R6 때문에 자동 재게시하지 않음 → needs_manual) |
| IN_PROGRESS | unknown |
| ERROR | rejected |
| EXPIRED | rejected (24시간이 지나 게시될 수 없음이 확정) |
- `not_found_is_definitive = False`, `supports_idempotency_key = False`

### Wave 4 인수 테스트 (FakeTransport)
- [ ] 정상: limit → container → rupload(offset/file_size 헤더) → FINISHED → publish → permalink → published
- [ ] 한도 초과 → failed, 컨테이너 생성 호출 없음
- [ ] 컨테이너 ERROR → failed, media_publish 호출 없음
- [ ] media_publish 타임아웃 → unknown + handle=container_id → retry에서 reconcile PUBLISHED → published, media_publish 재호출 없음
- [ ] reconcile FINISHED → needs_manual, 재게시 없음
- [ ] reconcile EXPIRED → failed → 다음 retry에서 **새 컨테이너**로 재제출
- [ ] 폴링 5분 초과 → unknown
- [ ] 라이브(opt-in): 테스트 계정에 1회 게시(Instagram은 비공개 게시 옵션이 없으므로 테스트 계정을 쓰거나 게시 후 수동 보관 처리)

---

## Wave 5 — TikTok inbox 어댑터 (`publishers/tiktok.py`)

### 인증
- Login Kit OAuth, 스코프는 `video.upload`(앱 승인 필요, R1).
- redirect URI 제약(R4)은 Wave 0 결과를 반영한다. localhost를 허용하지 않으면 별도 수동 코드 붙여넣기 흐름을 만든다.

### publish 흐름
1. 청크를 계산한다(media transfer guide 기준).
   - size < 5MB → chunk_size=size, total=1
   - 그 외 → chunk_size=10MB(5~64MB 범위), total=floor(size/chunk_size), 남는 바이트는 마지막 청크에 붙인다(마지막 청크 최대 128MB)
2. `POST https://open.tiktokapis.com/v2/post/publish/inbox/video/init/`
   body: `{"source_info": {"source": "FILE_UPLOAD", "video_size", "chunk_size", "total_chunk_count"}}`
   → `publish_id`와 `upload_url`(유효 1시간)을 받는다. 즉시 `on_handle(publish_id)`를 호출한다.
   - 4xx(`scope_not_authorized`, `spam_risk_too_many_pending_share` 등) → DefinitiveRejection
   - 429 `rate_limit_exceeded` → DefinitiveRejection(서버가 받지 않았다고 확정, 재시도 가능)
3. 청크 업로드: `PUT upload_url`을 순차로 보낸다. 헤더는 `Content-Type: video/mp4`, `Content-Length`, `Content-Range: bytes a-b/total`이다.
   - 도중 실패 → `TimeoutAfterAccept(publish_id)`
4. 상태 폴링: `POST /v2/post/publish/status/fetch/` `{"publish_id"}`를 보낸다(30 req/min 준수).
   - `SEND_TO_USER_INBOX` → `PublishResult(remote_id=publish_id, url=None, delivery="inbox")`
   - `FAILED` → DefinitiveRejection(fail_reason 포함)
   - 계속 `PROCESSING_UPLOAD`면 TimeoutAfterAccept(publish_id)
- metadata(caption)는 API로 보내지 않는다. CLI가 결과와 함께 캡션을 출력해 사용자가 앱에 붙여넣게 한다.

### reconcile(publish_id)
| status | 반환 |
|---|---|
| SEND_TO_USER_INBOX | published(delivery="inbox") |
| PUBLISH_COMPLETE | published(delivery="posted", `publicaly_available_post_id`가 있으면 URL 구성 — 필드명은 문서 철자 그대로) |
| PROCESSING_UPLOAD | unknown |
| FAILED | rejected. 단 fail_reason이 `auth_removed`, `spam_risk*`이면 `needs_manual`도 켜고 자동 재시도하지 않음 |
- `shortform status`는 delivery=inbox 레코드에 reconcile을 다시 호출해 PUBLISH_COMPLETE로 바뀌었는지 보여준다(조회만 하고 재제출하지 않음).

### Wave 5 인수 테스트 (FakeTransport)
- [ ] 3MB 파일 → 단일 청크, 23MB → chunk 10MB × 2(마지막 13MB), 각 Content-Range 정확
- [ ] 정상 → SEND_TO_USER_INBOX → published + delivery=inbox, CLI 출력에 "앱에서 게시" 안내와 캡션
- [ ] init 401 scope_not_authorized → failed, PUT 호출 없음
- [ ] 청크 2/3 업로드 실패 → unknown + handle=publish_id → retry reconcile SEND_TO_USER_INBOX → published, 재업로드 없음
- [ ] reconcile FAILED(internal) → failed → 다음 retry 재제출 / FAILED(spam_risk) → failed + needs_manual, 자동 재제출 없음
- [ ] 라이브(opt-in): 본인 계정 inbox 알림 수신 확인

---

## Wave 6 — 통합 마무리
- 사전 검증(`shortform/preflight.py`): 확장자와 크기(TikTok 4GB, YouTube 256GB)를 본다. `ffprobe`가 있으면 길이, fps, 해상도도 검사한다.
  예: TikTok 23~60fps·360~4096px, Shorts 3분 이하 세로. `ffprobe`가 없으면 경고만 낸다. 위반하면 dry-run 단계에서 거부한다.
- 플랫폼 하나의 인증이 만료돼도 나머지는 계속 진행된다(테스트로 검증).
- `README.md` 운영 가이드: 최초 설정, auth, 일상 사용, unknown/needs_manual 대응표.
- 3플랫폼 E2E 스모크 체크리스트(사람이 실행). 결과는 `docs/smoke-YYYY-MM-DD.md`에 기록.

### Wave 6 인수 테스트
- [ ] fps 70 영상 → dry-run에서 TikTok 거부, 다른 플랫폼 계획은 출력
- [ ] YouTube 토큰 만료 + IG/TikTok 정상 → YouTube failed(AuthRequired), 나머지 published
- [ ] 라이브 3플랫폼 동시 1회(private/inbox)
