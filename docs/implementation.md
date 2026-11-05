# 현재 구현과 확인 범위

사용자 확정 요구: 로컬 MP4 하나를 한 CLI 실행으로 YouTube Shorts·Instagram Reels에 게시하고,
TikTok에는 inbox 업로드까지만 자동으로 수행한다. TikTok 최종 게시·캡션 입력은 사용자가 한다.
기존 PRD/BE 계획은 설계 참고 자료이며 Wave별 PR 생성이나 Git 게시 작업은 수행하지 않았다.

## 구현

- strict TOML 설정, 영상·메타데이터 승인 바인딩, 표준 라이브러리 HTTP 전송
- 외부 설정 디렉터리의 0600 원자적 토큰 저장, OAuth state 검증, YouTube PKCE
- YouTube resumable 업로드·재개, Instagram resumable 컨테이너 게시, TikTok inbox 청크 업로드
- 복구 handle 즉시 저장, 프로세스 종료 후 unknown 복구, 원격 확인 우선 retry
- CLI status 조회, TikTok inbox 안내·캡션 출력, job별 프로세스 잠금
- 파일/용량 검사와 선택적 ffprobe 검사, README 운영 안내

## 설계 문서에서 구체화한 부분

- Instagram·TikTok의 OAuth는 등록된 redirect URI에 도착한 전체 URL을 터미널에 숨김 입력한다.
  YouTube만 임의 로컬 포트 루프백을 사용한다. 로컬 콜백을 허용한다고 가정하지 않는다.
- Facebook Login for Business의 `config_id`와 `pages_show_list`를 포함한다.
- Instagram은 장기 사용자 토큰에서 페이지 토큰을 조회해 사용한다. 일반적인 refresh token 방식이나
  장기 토큰 반복 교환을 갱신 보장으로 간주하지 않는다. 기록된 만료 시 재로그인한다.
- YouTube 복구 handle은 세션 URI·전체 크기·계정 ID를 함께 기록해 재시작 후 상태 조회가 가능하다.
  base64 포장은 암호화가 아니며 job 파일은 비공개로 관리해야 한다.
- 업로드된 미확인 영상을 status/retry에서 새로 제출하지 않는다. 중단된 YouTube 세션이 308이면
  수동 확인 대상으로 남긴다. 같은 실행 내 재개만 지원한다.
- TikTok 청크 개수는 크기/기준 청크 크기의 내림값으로 계산하고 나머지 바이트는 최종 청크에 합친다.
- 새 `run`은 새 job이다. 서로 다른 job 사이에 같은 영상을 게시하는 것까지 막지는 않는다.

## 공식 근거와 남은 확인

- [YouTube resumable protocol](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol)
- [YouTube videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert)
- [Meta 운영 Instagram API 문서](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api)
- [Meta 페이지 토큰 조회](https://www.postman.com/meta/instagram/request/0vuw3vk/get-access-tokens-of-pages-you-manage)
- [Instagram publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/)
- [TikTok inbox API](https://developers.tiktok.com/doc/content-posting-api-reference-upload-video)
- [TikTok media transfer](https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide)
- [TikTok status API](https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status)

Meta 개발자 문서 일부는 HTTP 429로 원문 열람이 제한됐다. Meta 운영 Postman에서 권한과 페이지 토큰
조회 흐름을 확인했지만 Facebook Login for Business 페이지 토큰의 현재 수명·갱신 보장까지는
확인하지 못했다. 자동 갱신을 가정하지 않고 재인증으로 처리했다. 이 제한과 앱 심사 필요 여부,
개별 앱에서 Instagram 로컬 resumable 업로드가 가능한지는 실제 앱 테스트로 확인해야 한다.

단위 테스트는 실제 플랫폼 연결·심사 승인·계정 게시 성공의 증거가 아니다.
라이브 검증은 `smoke-checklist.md`에 미실행으로 명시했다.
