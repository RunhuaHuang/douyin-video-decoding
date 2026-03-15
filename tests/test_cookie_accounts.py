import json

from app.cookie_accounts import CookieAccountManager


def test_cookie_account_manager_ignores_legacy_default_by_default(tmp_path):
    accounts_dir = tmp_path / "browser" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    legacy_storage_path = tmp_path / "browser" / "legacy.json"
    legacy_storage_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_storage_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "ttwid",
                        "value": "cookie",
                        "domain": ".douyin.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (accounts_dir / "__legacy_default__.meta.json").write_text(
        json.dumps(
            {
                "account_id": "__legacy_default__",
                "label": "默认会话",
                "source": "legacy",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manager = CookieAccountManager(
        accounts_dir=accounts_dir,
        legacy_storage_state_path=legacy_storage_path,
    )

    assert manager.list_accounts() == []
    assert manager.list_available_accounts() == []


def test_cookie_account_manager_migrates_legacy_auto_invalid_to_warning(tmp_path):
    accounts_dir = tmp_path / "browser" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)

    (accounts_dir / "demo.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "ttwid",
                        "value": "cookie",
                        "domain": ".douyin.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (accounts_dir / "demo.meta.json").write_text(
        json.dumps(
            {
                "account_id": "demo",
                "label": "demo",
                "invalid": True,
                "invalid_reason": "连续 3 次命中强风控/验证码：强冷却信号：验证码挑战页",
                "invalidated_at": "2026-03-13T15:08:28.187364+00:00",
                "challenge_strikes": 3,
                "source": "managed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manager = CookieAccountManager(
        accounts_dir=accounts_dir,
        legacy_storage_state_path=tmp_path / "browser" / "legacy.json",
    )
    account = manager.get_account("demo")

    assert account is not None
    assert account.invalid is False
    assert account.warning is True
    assert account.warning_reason == "连续 3 次命中强风控/验证码：强冷却信号：验证码挑战页"
