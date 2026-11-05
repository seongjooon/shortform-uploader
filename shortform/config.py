"""Strict loading for the local ``shortform.toml`` configuration file."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import tomllib

from .models import PlatformTarget


_DEFAULT_KEYS = frozenset({"title", "description", "caption"})
_PLATFORM_KEYS = {
    "youtube": frozenset({"enabled", "account_id", "privacy", "title", "description", "made_for_kids"}),
    "instagram": frozenset({"enabled", "account_id", "caption", "share_to_feed"}),
    "tiktok": frozenset({"enabled", "account_id", "caption", "privacy"}),
}
_YOUTUBE_PRIVACY = frozenset({"private", "unlisted", "public"})
_TIKTOK_PRIVACY = frozenset({"inbox", "public", "followers", "friends", "private"})
_SECRET_MARKERS = ("secret", "token", "password", "api_key", "apikey", "credential")


def _error(path: str, message: str) -> ValueError:
    return ValueError(f"shortform.toml {path}: {message}")


def _require_table(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "테이블이어야 합니다")
    return value


def _validate_keys(table: dict[str, Any], allowed: frozenset[str], path: str) -> None:
    for key in table:
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            raise _error(f"{path}.{key}", "비밀값은 설정 파일에 둘 수 없습니다")
        if key not in allowed:
            raise _error(f"{path}.{key}", "알 수 없는 키입니다")


def _string(value: Any, path: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str):
        raise _error(path, "문자열이어야 합니다")
    if nonempty and not value.strip():
        raise _error(path, "비어 있을 수 없습니다")
    return value


def _boolean(value: Any, path: str) -> bool:
    # bool is a subclass of int, so an exact type check is intentional here.
    if type(value) is not bool:
        raise _error(path, "true 또는 false여야 합니다")
    return value


def _metadata_value(table: dict[str, Any], defaults: dict[str, str], key: str, path: str) -> str | None:
    if key in table:
        return _string(table[key], f"{path}.{key}")
    return defaults.get(key)


def _load_platform(name: str, table: dict[str, Any], defaults: dict[str, str]) -> PlatformTarget | None:
    path = f"platforms.{name}"
    _validate_keys(table, _PLATFORM_KEYS[name], path)

    if "enabled" not in table:
        raise _error(f"{path}.enabled", "필수 키입니다")
    if not _boolean(table["enabled"], f"{path}.enabled"):
        return None
    if "account_id" not in table:
        raise _error(f"{path}.account_id", "필수 키입니다")
    account_id = _string(table["account_id"], f"{path}.account_id", nonempty=True)

    if name == "youtube":
        privacy = table.get("privacy", "private")
        privacy = _string(privacy, f"{path}.privacy")
        if privacy not in _YOUTUBE_PRIVACY:
            raise _error(f"{path}.privacy", "private, unlisted, public 중 하나여야 합니다")
        metadata: dict[str, Any] = {}
        for key in ("title", "description"):
            value = _metadata_value(table, defaults, key, path)
            if value is not None:
                metadata[key] = value
        if "made_for_kids" in table:
            metadata["made_for_kids"] = _boolean(table["made_for_kids"], f"{path}.made_for_kids")
        return PlatformTarget(provider="youtube", account_id=account_id, privacy=privacy, metadata=metadata)

    caption = _metadata_value(table, defaults, "caption", path)
    metadata = {} if caption is None else {"caption": caption}
    if name == "instagram":
        if "share_to_feed" in table:
            metadata["share_to_feed"] = _boolean(table["share_to_feed"], f"{path}.share_to_feed")
        return PlatformTarget(provider="instagram", account_id=account_id, privacy="account", metadata=metadata)
    privacy = _string(table.get("privacy", "inbox"), f"{path}.privacy")
    if privacy not in _TIKTOK_PRIVACY:
        raise _error(f"{path}.privacy", "inbox, public, followers, friends, private 중 하나여야 합니다")
    return PlatformTarget(provider="tiktok", account_id=account_id, privacy=privacy, metadata=metadata)


def load_config(path: str | Path) -> dict[str, PlatformTarget]:
    """Load enabled platform targets, rejecting ambiguous or secret-bearing input.

    Secrets belong in the local token store, never in a project configuration file.
    Disabled platforms are validated but omitted from the returned publish targets.
    """
    config_path = Path(path)
    try:
        with config_path.open("rb") as source:
            raw = tomllib.load(source)
    except FileNotFoundError as exc:
        raise ValueError(f"shortform.toml 파일이 없습니다: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"shortform.toml 형식 오류: {exc}") from exc

    _validate_keys(raw, frozenset({"defaults", "platforms"}), "")
    defaults_raw = _require_table(raw.get("defaults", {}), "defaults")
    _validate_keys(defaults_raw, _DEFAULT_KEYS, "defaults")
    defaults = {key: _string(value, f"defaults.{key}") for key, value in defaults_raw.items()}

    if "platforms" not in raw:
        raise _error("platforms", "필수 테이블입니다")
    platforms = _require_table(raw["platforms"], "platforms")
    targets: dict[str, PlatformTarget] = {}
    for name, table in platforms.items():
        if name not in _PLATFORM_KEYS:
            raise _error(f"platforms.{name}", "지원하지 않는 플랫폼입니다")
        target = _load_platform(name, _require_table(table, f"platforms.{name}"), defaults)
        if target is not None:
            targets[name] = target
    return targets
