"""Local, side-effect-free video checks before a live upload is attempted."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess

from .models import PlatformTarget

_GIB = 1024**3
_TIKTOK_MAX_BYTES = 4 * _GIB
_YOUTUBE_MAX_BYTES = 256 * _GIB


@dataclass(frozen=True)
class PreflightResult:
    """Blocking errors by platform, plus non-blocking inspection warnings."""

    errors: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(self.errors.values())


def _probe(path: Path) -> tuple[dict[str, object] | None, str | None]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,r_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except FileNotFoundError:
        return None, "ffprobe를 찾을 수 없어 길이·fps·해상도 검사를 건너뛰었습니다"
    except subprocess.TimeoutExpired:
        return None, "ffprobe 시간이 초과되어 길이·fps·해상도 검사를 건너뛰었습니다"
    except OSError as exc:
        return None, f"ffprobe 실행 실패({exc}); 길이·fps·해상도 검사를 건너뛰었습니다"
    if completed.returncode != 0:
        return None, "ffprobe가 영상을 읽지 못해 길이·fps·해상도 검사를 건너뛰었습니다"
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, "ffprobe 출력이 올바르지 않아 길이·fps·해상도 검사를 건너뛰었습니다"
    if not isinstance(payload, dict):
        return None, "ffprobe 출력이 올바르지 않아 길이·fps·해상도 검사를 건너뛰었습니다"
    return payload, None


def _rate(value: object) -> float | None:
    if not isinstance(value, str) or not value or value == "0/0":
        return None
    try:
        numerator, denominator = value.split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else None
    except (TypeError, ValueError):
        return None


def _video_details(payload: dict[str, object]) -> tuple[float | None, int | None, int | None, float | None]:
    duration: float | None = None
    fmt = payload.get("format")
    if isinstance(fmt, dict):
        try:
            duration = float(fmt.get("duration"))
        except (TypeError, ValueError):
            pass
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return duration, None, None, None
    stream = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), None)
    if not isinstance(stream, dict):
        return duration, None, None, None
    width = stream.get("width")
    height = stream.get("height")
    return duration, width if type(width) is int else None, height if type(height) is int else None, _rate(stream.get("r_frame_rate"))


def check_video(path: str | Path, targets: Mapping[str, PlatformTarget]) -> PreflightResult:
    """Check local file constraints for each selected live platform.

    The function does not access the network and treats missing ``ffprobe`` as a
    warning so a caller can still show the upload plan on a minimal installation.
    """
    video = Path(path)
    platforms = list(targets)
    errors: dict[str, list[str]] = {platform: [] for platform in platforms}
    warnings: list[str] = []

    if not video.is_file():
        message = f"영상 파일이 없습니다: {video}"
        return PreflightResult({platform: [message] for platform in platforms}, warnings)
    if video.suffix.lower() != ".mp4":
        message = "MP4 파일만 업로드할 수 있습니다"
        return PreflightResult({platform: [message] for platform in platforms}, warnings)
    size = video.stat().st_size
    if size <= 0:
        message = "영상 파일이 비어 있습니다"
        return PreflightResult({platform: [message] for platform in platforms}, warnings)

    if "tiktok" in errors and size > _TIKTOK_MAX_BYTES:
        errors["tiktok"].append("TikTok은 4GB 이하 영상만 지원합니다")
    if "youtube" in errors and size > _YOUTUBE_MAX_BYTES:
        errors["youtube"].append("YouTube는 256GB 이하 영상만 지원합니다")

    payload, warning = _probe(video)
    if warning:
        warnings.append(warning)
        return PreflightResult(errors, warnings)

    duration, width, height, fps = _video_details(payload or {})
    if "youtube" in errors:
        if duration is not None and duration > 180:
            errors["youtube"].append("YouTube Shorts는 180초 이하 영상이어야 합니다")
        if width is not None and height is not None and height < width:
            errors["youtube"].append("YouTube Shorts는 세로 또는 정사각형 영상이어야 합니다")
    if "tiktok" in errors:
        if fps is not None and not 23 <= fps <= 60:
            errors["tiktok"].append("TikTok은 23~60fps 영상이어야 합니다")
        if width is not None and height is not None and not (360 <= width <= 4096 and 360 <= height <= 4096):
            errors["tiktok"].append("TikTok 해상도는 가로·세로 각각 360~4096px이어야 합니다")
    return PreflightResult(errors, warnings)
