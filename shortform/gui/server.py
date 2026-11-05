"""Local-only browser interface for the shared upload service."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import service
from ..auth import TokenStore

MAX_VIDEO = 4 * 1024 ** 3
MAX_JSON = 64 * 1024


class App:
    def __init__(self, directory=None, store=None, config_path=None, publishers_factory=service.publishers):
        self.directory = Path(directory or tempfile.mkdtemp(prefix="shortform-gui-"))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = store or TokenStore()
        self.config_path = config_path
        self.publishers_factory = publishers_factory
        self.token = secrets.token_urlsafe(32)
        self.videos = {}
        self.plans = {}
        self.uploads = {}
        self.lock = threading.Lock()

    def targets(self):
        return service.available_targets(self.config_path, self.store)

    def run_upload(self, upload_id, plan):
        state = self.uploads[upload_id]
        def created(job):
            with self.lock:
                state["job_id"] = job.id
        def started(platform):
            with self.lock:
                state["platforms"][platform] = {"state": "publishing"}
        def done(platform, rec):
            with self.lock:
                state["platforms"][platform] = {"state": rec.state, "url": rec.url, "error": rec.error,
                                                  "delivery": rec.delivery, "note": rec.note}
        try:
            service.upload(plan, publishers_factory=self.publishers_factory, on_created=created, on_done=done, on_start=started)
        except Exception:
            with self.lock:
                state["error"] = "업로드를 시작하지 못했습니다. 작업 기록을 확인하세요."
        finally:
            with self.lock:
                state["done"] = True


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass  # URLs contain the one-time browser token.

        def reply(self, status, body, content_type="application/json; charset=utf-8", headers=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, mutation=False):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != host:
                return False
            if mutation and self.headers.get("Origin") != f"http://{host}":
                return False
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                return False
            value = cookie.get("shortform_session")
            return bool(value and secrets.compare_digest(value.value, app.token))

        def length(self, maximum):
            try:
                n = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                n = -1
            if n < 0 or n > maximum:
                raise ValueError("요청 크기가 올바르지 않습니다")
            return n

        def do_GET(self):
            url = urlsplit(self.path)
            host = f"127.0.0.1:{self.server.server_port}"
            if url.path == "/" and self.headers.get("Host") == host:
                token = parse_qs(url.query).get("t", [""])[0]
                if secrets.compare_digest(token, app.token):
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", f"shortform_session={app.token}; HttpOnly; SameSite=Strict; Path=/")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            if not self.allowed():
                return self.reply(403, {"error": "접근할 수 없습니다"})
            if url.path in ("/", "/app.js", "/style.css"):
                name = "index.html" if url.path == "/" else url.path[1:]
                mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
                return self.reply(200, (Path(__file__).parent / name).read_bytes(), mime + "; charset=utf-8")
            if url.path == "/api/accounts":
                try:
                    targets, unavailable = app.targets()
                    return self.reply(200, {"available": list(targets), "unavailable": unavailable})
                except (ValueError, OSError):
                    return self.reply(400, {"error": "계정 설정을 읽지 못했습니다"})
            if url.path.startswith("/api/uploads/"):
                upload_id = url.path.removeprefix("/api/uploads/")
                with app.lock:
                    state = app.uploads.get(upload_id)
                    data = json.loads(json.dumps(state)) if state else None
                return self.reply(200, data) if data else self.reply(404, {"error": "작업을 찾을 수 없습니다"})
            self.reply(404, {"error": "찾을 수 없습니다"})

        def do_POST(self):
            if not self.allowed(mutation=True):
                self.close_connection = True
                return self.reply(403, {"error": "접근할 수 없습니다"})
            try:
                if self.path == "/api/videos":
                    return self.receive_video()
                length = self.length(MAX_JSON)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("요청이 올바르지 않습니다")
                if self.path == "/api/plans":
                    return self.prepare(data)
                if self.path == "/api/uploads":
                    return self.start(data)
                self.reply(404, {"error": "찾을 수 없습니다"})
            except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError):
                self.close_connection = True
                self.reply(400, {"error": "요청을 처리하지 못했습니다. 입력과 영상 파일을 확인하세요."})

        def receive_video(self):
            length = self.length(MAX_VIDEO)
            if not length:
                raise ValueError("빈 파일")
            video_id = secrets.token_urlsafe(18)
            path = app.directory / f"{video_id}.mp4"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("파일 전송이 중단됐습니다")
                        f.write(chunk)
                        remaining -= len(chunk)
                targets, _ = app.targets()
                inspection = service.inspect(path, targets)
                app.videos[video_id] = path
                self.reply(200, {"id": video_id, **asdict(inspection)})
            except BaseException:
                path.unlink(missing_ok=True)
                raise

        def prepare(self, data):
            video = app.videos[data["video_id"]]
            targets, _ = app.targets()
            caption, tags, title, platforms = (data[k] for k in ("caption", "hashtags", "title", "platforms"))
            if not all(isinstance(x, str) for x in (caption, tags, title)) or not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
                raise ValueError("잘못된 입력")
            plan = service.plan(video, caption, service.parse_hashtags(tags), platforms, title, targets)
            checks = service.inspect(video, plan.targets).checks
            if not checks.ok:
                return self.reply(400, {"error": "영상 사전 검사에 실패했습니다", "checks": asdict(checks)})
            plan_id = secrets.token_urlsafe(18)
            with app.lock:
                app.plans[plan_id] = plan
            self.reply(200, {"plan_id": plan_id, "targets": {p: {"account": t.account_id, "privacy": t.privacy, "metadata": t.metadata} for p, t in plan.targets.items()}, "warnings": checks.warnings})

        def start(self, data):
            if data.get("confirmed") is not True:
                raise ValueError("승인이 필요합니다")
            with app.lock:
                plan = app.plans.pop(data["plan_id"], None)
            if plan is None:
                raise ValueError("계획이 없거나 이미 사용됐습니다")
            targets, _ = app.targets()
            if any(p not in targets or targets[p].account_id != t.account_id or targets[p].privacy != t.privacy for p, t in plan.targets.items()):
                raise ValueError("계정 설정이 바뀌었습니다")
            if not service.inspect(plan.video, plan.targets).checks.ok:
                raise ValueError("영상 사전 검사에 실패했습니다")
            upload_id = secrets.token_urlsafe(18)
            state = {"done": False, "job_id": None, "error": None,
                     "platforms": {p: {"state": "queued"} for p in plan.targets}}
            with app.lock:
                app.uploads[upload_id] = state
            threading.Thread(target=app.run_upload, args=(upload_id, plan), daemon=True).start()
            self.reply(202, {"id": upload_id})

    return Handler


def run():
    config = Path("shortform.toml")
    app = App(config_path=config if config.is_file() else None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    url = f"http://127.0.0.1:{server.server_port}/?t={app.token}"
    print("브라우저에서 업로드 화면을 엽니다. 종료하려면 Ctrl+C를 누르세요.", flush=True)
    try:
        opened = webbrowser.open(url)
    except webbrowser.Error:
        opened = False
    if not opened:
        print(f"브라우저에 이 주소를 입력하세요: {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()
