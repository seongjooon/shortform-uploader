from __future__ import annotations

import pytest

from shortform.config import load_config


def _write(tmp_path, text: str):
    path = tmp_path / "shortform.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_merges_defaults_and_normalizes_platform_privacy(tmp_path):
    path = _write(
        tmp_path,
        '''[defaults]
title = "기본 제목"
description = "기본 설명"
caption = "기본 캡션"

[platforms.youtube]
enabled = true
account_id = "UC1"
privacy = "unlisted"
made_for_kids = false

[platforms.instagram]
enabled = true
account_id = "ig1"
share_to_feed = true

[platforms.tiktok]
enabled = true
account_id = "tt1"
''',
    )

    targets = load_config(path)

    assert targets["youtube"].privacy == "unlisted"
    assert targets["youtube"].metadata == {"title": "기본 제목", "description": "기본 설명", "made_for_kids": False}
    assert targets["instagram"].privacy == "account"
    assert targets["instagram"].metadata == {"caption": "기본 캡션", "share_to_feed": True}
    assert targets["tiktok"].privacy == "inbox"


@pytest.mark.parametrize(
    "text, match",
    [
        ("[platforms.youtube]\nenabled = true\naccount_id = 'UC'\nprivacy = 'friends'", "privacy"),
        ("[platforms.youtube]\nenabled = 'yes'\naccount_id = 'UC'", "enabled"),
        ("[platforms.youtube]\nenabled = true\naccount_id = ''", "account_id"),
        ("[platforms.youtube]\nenabled = true\naccount_id = 'UC'\naccess_token = 'secret'", "비밀값"),
        ("[platforms.unknown]\nenabled = true\naccount_id = 'x'", "지원하지 않는 플랫폼"),
    ],
)
def test_load_config_rejects_invalid_or_unsafe_input(tmp_path, text, match):
    with pytest.raises(ValueError, match=match):
        load_config(_write(tmp_path, text))


def test_disabled_platform_is_validated_but_not_returned(tmp_path):
    path = _write(
        tmp_path,
        "[platforms.youtube]\nenabled = false\naccount_id = 'UC'\nprivacy = 'private'\n",
    )
    assert load_config(path) == {}
