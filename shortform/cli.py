"""Local, approval-bound multi-platform uploader."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import pipeline, service
from .models import PUBLISHED, PlatformTarget
from .publishers.base import DefinitiveRejection

EXIT_OK = 0
EXIT_NOT_ALL_PUBLISHED = 1
EXIT_INVALID = 2


def _build_parser():
    parser = argparse.ArgumentParser(prog="shortform-uploader", description="인자 없이 실행하면 대화형 업로드를 시작합니다.",
                                     epilog="브라우저 화면: shortform-uploader gui · 계정 연결: shortform-uploader setup")
    sub = parser.add_subparsers(dest="command")
    upload = sub.add_parser("upload", help="대화형 업로드 (영상 끌어다 놓기, 캡션, 플랫폼 선택)")
    upload.add_argument("video", nargs="?", help="영상 파일 (생략하면 실행 후 끌어다 놓기)")
    upload.add_argument("--config", help="실제 계정용 TOML 설정 (없으면 연결된 계정 기본값 사용)")
    upload.add_argument("--jobs-dir", default=None)
    sub.add_parser("gui", help="브라우저 업로드 화면 열기")
    sub.add_parser("accounts", help="계정 연결 상태 확인")
    setup = sub.add_parser("setup", help="처음 쓰는 사람을 위한 단계별 계정 연결 도우미")
    setup.add_argument("platforms", nargs="*", choices=("youtube", "instagram", "tiktok"), metavar="platform",
                       help="연결할 플랫폼 (생략하면 목록에서 선택)")
    run = sub.add_parser("run", help="영상 게시 (승인 후)")
    run.add_argument("video")
    run.add_argument("--config", help="실제 계정용 TOML 설정")
    run.add_argument("--only", help="설정 중 실행할 플랫폼 (쉼표 구분)")
    run.add_argument("--title")
    run.add_argument("--caption")
    run.add_argument("--description")
    run.add_argument("--platforms", help="테스트용 mockA,mockB")
    run.add_argument("--account")
    run.add_argument("--privacy")
    run.add_argument("--jobs-dir", default=None)
    run.add_argument("--dry-run", action="store_true")
    auth = sub.add_parser("auth", help="플랫폼 OAuth 로그인")
    auth.add_argument("platform", choices=("youtube", "instagram", "tiktok"))
    for command in ("status", "retry"):
        p = sub.add_parser(command)
        p.add_argument("job_id")
        p.add_argument("--jobs-dir", default=None)
    return parser


def _parse_platforms(raw):
    platforms = [p.strip() for p in raw.split(",") if p.strip()]
    if not platforms or len(set(platforms)) != len(platforms):
        raise ValueError("플랫폼 목록이 비었거나 중복됐습니다")
    if any(not p.startswith("mock") for p in platforms):
        raise ValueError("실제 플랫폼은 --config로 지정하세요")
    return platforms


def _build_targets(args, platforms):
    metadata = {"title": args.title, "caption": args.caption or "", "description": args.description or ""}
    return {p: PlatformTarget("mock", args.account or "me", args.privacy or "private", dict(metadata)) for p in platforms}


def _print_plan(video, targets):
    print(f"영상: {video.resolve()}")
    print("게시 계획:")
    for platform, t in targets.items():
        print(f"  - {platform}: provider={t.provider} account={t.account_id} privacy={t.privacy}")
        for key, value in t.metadata.items():
            print(f"      {key}: {value!r}")


_publishers = service.publishers


def _print_result(job, jobs_dir):
    print(f"job: {pipeline.job_path(job.id, jobs_dir)}")
    for platform, rec in job.records.items():
        flag = " (needs_manual: 수동 확인 필요)" if rec.needs_manual else ""
        delivery = f" delivery={rec.delivery}" if rec.delivery else ""
        print(f"  - {platform}: {rec.state}{delivery}{flag} {rec.url or rec.error or ''}".rstrip())
        if rec.delivery == "inbox":
            print("    TikTok 앱 받은편지함에서 영상을 열고 앱에서 게시하세요.")
            print(f"    붙여넣을 캡션: {job.targets[platform].metadata.get('caption', '')}")
    return EXIT_OK if all(r.state == PUBLISHED for r in job.records.values()) else EXIT_NOT_ALL_PUBLISHED


def _select_platforms_interactive(targets: dict[str, PlatformTarget]) -> dict[str, PlatformTarget]:
    platform_names = list(targets.keys())
    if len(platform_names) <= 1:
        return targets

    display_names = {
        "youtube": "YouTube Shorts",
        "instagram": "Instagram Reels",
        "tiktok": "TikTok Inbox",
    }

    print("\n업로드할 SNS 플랫폼을 선택하세요:")
    for idx, name in enumerate(platform_names, 1):
        label = display_names.get(name, name)
        print(f"  {idx}. {label} ({name})")
    print("  a. 전체 선택 (All)")

    while True:
        try:
            choice = input("선택 (번호/이름 쉼표 구분, Enter: 전체) [기본: 전체]: ").strip()
        except EOFError:
            return targets

        if not choice or choice.lower() in ("a", "all", "y", "yes"):
            return targets

        selected = []
        tokens = [t.strip().lower() for t in choice.split(",") if t.strip()]
        valid = True
        for token in tokens:
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(platform_names):
                    p = platform_names[idx - 1]
                    if p not in selected:
                        selected.append(p)
                else:
                    valid = False
                    break
            elif token in targets:
                if token not in selected:
                    selected.append(token)
            else:
                valid = False
                break

        if valid and selected:
            return {p: targets[p] for p in selected}

        print("잘못된 선택입니다. 번호(예: 1,3) 또는 플랫폼 이름을 입력하세요.")


def _cmd_run(args):
    video = Path(args.video)
    if not video.is_file():
        raise ValueError(f"영상 파일이 없습니다: {video}")
    config_file = args.config
    if not config_file and not args.platforms and Path("shortform.toml").is_file():
        config_file = "shortform.toml"
    if config_file:
        if any(getattr(args, key) is not None for key in ("platforms", "title", "caption", "description", "account", "privacy")):
            raise ValueError("--config와 mock용 게시 옵션을 함께 쓸 수 없습니다")
        from .config import load_config
        targets = load_config(config_file)
        if args.only:
            selected = args.only.split(",")
            if any(p not in targets for p in selected) or len(set(selected)) != len(selected):
                raise ValueError("--only에는 설정에서 활성화한 플랫폼만 지정하세요")
            targets = {p: targets[p] for p in selected}
        elif not args.dry_run:
            targets = _select_platforms_interactive(targets)
        if not targets:
            raise ValueError("활성화한 플랫폼이 없습니다")
    else:
        if args.only or not args.platforms or not args.title or not args.title.strip():
            raise ValueError("--config 또는 --title과 --platforms(mock)를 지정하세요")
        targets = _build_targets(args, _parse_platforms(args.platforms))
    _print_plan(video, targets)
    errors = {}
    if config_file:
        from .preflight import check_video
        result = check_video(video, targets)
        errors = {p: reasons for p, reasons in result.errors.items() if reasons}
        for warning in result.warnings:
            print(f"경고: {warning}", file=sys.stderr)
        for p, reasons in errors.items():
            print(f"검증 실패 [{p}]: {'; '.join(reasons)}", file=sys.stderr)
    if args.dry_run:
        print("[dry-run] 승인/게시 없이 계획을 확인했습니다.")
        return EXIT_INVALID if errors else EXIT_OK
    if errors:
        raise ValueError("영상 사전 검증에 실패했습니다. 영상을 수정하거나 --only로 대상을 선택하세요")
    if input("위 내용으로 게시를 승인합니까? [y/N] ").strip().lower() not in ("y", "yes"):
        print("승인되지 않아 종료합니다.")
        return EXIT_OK
    job = service.upload(service.UploadPlan(video, targets), args.jobs_dir, publishers_factory=_publishers)
    return _print_result(job, args.jobs_dir)


def _cmd_upload(args):
    from .auth import TokenStore
    from .interactive import run_wizard
    config = getattr(args, "config", None)
    if config is None and Path("shortform.toml").is_file():
        config = "shortform.toml"
    return run_wizard(
        publishers_factory=_publishers,
        store=TokenStore(),
        config_path=Path(config) if config else None,
        jobs_dir=getattr(args, "jobs_dir", None),
        video=getattr(args, "video", None),
    )


def _cmd_accounts():
    from .auth import TokenStore
    from .interactive import LABELS, PLATFORMS
    store = TokenStore()
    connected = service.accounts(store)
    for platform in PLATFORMS:
        account = connected[platform]
        status = f"연결됨 (계정 {account})" if account else f"미연결 → shortform-uploader setup {platform}"
        print(f"{LABELS[platform]}: {status}")
    return EXIT_OK


_COMMANDS = frozenset({"gui", "upload", "accounts", "setup", "run", "auth", "status", "retry"})


def main(argv: Sequence[str] | None = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # `shortform-uploader <영상>` (typed, then the file dragged in) starts the upload wizard with that video.
    if argv and argv[0] not in _COMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "upload")
    args = _build_parser().parse_args(argv)
    try:
        if args.command in (None, "upload"):
            return _cmd_upload(args)
        if args.command == "gui":
            from .gui.server import run
            run()
            return EXIT_OK
        if args.command == "accounts":
            return _cmd_accounts()
        if args.command == "setup":
            from .setup import run_setup
            return run_setup(args.platforms or None)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "auth":
            service.connect(args.platform)
            print(f"{args.platform}: 인증 정보를 로컬에 저장했습니다.")
            return EXIT_OK
        operation = service.status if args.command == "status" else service.retry
        job = operation(args.job_id, args.jobs_dir, publishers_factory=_publishers)
        return _print_result(job, args.jobs_dir)
    except (ValueError, OSError, RuntimeError, DefinitiveRejection) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except EOFError:
        print("오류: 대화형 승인 입력이 필요합니다", file=sys.stderr)
        return EXIT_INVALID
    except KeyboardInterrupt:
        print("\n취소했습니다.", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
