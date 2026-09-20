from __future__ import annotations

import json
from pathlib import Path

from app import login_session


class _DummyPage:
    def __init__(self) -> None:
        self.goto_calls = []
        self._closed = False
        self.handlers = {}

    def goto(self, url: str, wait_until: str = "") -> None:
        self.goto_calls.append((url, wait_until))

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        return None

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def is_closed(self) -> bool:
        return self._closed


class _DummyContext:
    def __init__(self) -> None:
        self.clear_cookies_called = False
        self.init_scripts = []
        self.page = _DummyPage()
        self.storage_state_path = None

    def clear_cookies(self) -> None:
        self.clear_cookies_called = True

    def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    def new_page(self):
        return self.page

    def storage_state(self, path: str) -> None:
        self.storage_state_path = path
        Path(path).write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "ttwid",
                            "value": "persisted",
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

    def close(self) -> None:
        return None

    @property
    def pages(self):
        return [] if self.page.is_closed() else [self.page]


class _DummyBrowser:
    def __init__(self) -> None:
        self.new_context_calls = []
        self.context = _DummyContext()
        self._connected = True

    def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        return self.context

    def close(self) -> None:
        self._connected = False
        return None

    def is_connected(self) -> bool:
        return self._connected and not self.context.page.is_closed()


class _DummyChromium:
    def __init__(self) -> None:
        self.browser = _DummyBrowser()

    def launch(self, headless: bool = False):
        return self.browser


class _DummyPlaywright:
    def __init__(self) -> None:
        self.chromium = _DummyChromium()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _DummySyncPlaywright:
    def __init__(self) -> None:
        self.instance = _DummyPlaywright()

    def __call__(self):
        return self.instance


def test_login_session_starts_with_clean_context(tmp_path, monkeypatch):
    account_path = tmp_path / "browser" / "accounts" / "main.json"
    account_path.parent.mkdir(parents=True, exist_ok=True)
    account_path.write_text('{"cookies":[{"name":"ttwid","value":"old"}],"origins":[]}', encoding="utf-8")

    dummy_sync_playwright = _DummySyncPlaywright()

    class DummyAccount:
        account_id = "main"
        label = "主号"
        storage_state_path = account_path

    class DummyAccountManager:
        def prepare_account(self, label: str):
            return DummyAccount()

        def mark_relogin_in_progress(self, account_id: str, active: bool):
            return DummyAccount()

        def record_login(self, account_id: str, *, label: str | None = None):
            return DummyAccount()

    monkeypatch.setattr(login_session, "CookieAccountManager", lambda: DummyAccountManager())
    monkeypatch.setattr(login_session, "ensure_directories", lambda: None)
    monkeypatch.setattr(login_session, "_resolve_account_label", lambda argv: "主号")
    monkeypatch.setattr("playwright.sync_api.sync_playwright", dummy_sync_playwright)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    login_session.run()

    browser = dummy_sync_playwright.instance.chromium.browser
    context_kwargs = browser.new_context_calls[0]

    assert "storage_state" not in context_kwargs
    assert browser.context.clear_cookies_called is True
    assert browser.context.init_scripts
    assert account_path.exists()
    assert browser.context.storage_state_path.endswith(".tmp.json")


def test_login_session_browser_close_mode_finishes_and_persists(tmp_path, monkeypatch):
    account_path = tmp_path / "browser" / "accounts" / "main.json"
    account_path.parent.mkdir(parents=True, exist_ok=True)

    dummy_sync_playwright = _DummySyncPlaywright()

    class DummyAccount:
        account_id = "main"
        label = "主号"
        storage_state_path = account_path

    class DummyAccountManager:
        def prepare_account(self, label: str):
            return DummyAccount()

        def mark_relogin_in_progress(self, account_id: str, active: bool):
            return DummyAccount()

        def record_login(self, account_id: str, *, label: str | None = None):
            return DummyAccount()

    monkeypatch.setattr(login_session, "CookieAccountManager", lambda: DummyAccountManager())
    monkeypatch.setattr(login_session, "ensure_directories", lambda: None)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", dummy_sync_playwright)
    dummy_sync_playwright.instance.chromium.browser.context.page._closed = True

    login_session.login_with_label("主号", wait_mode="browser_close", logger=lambda message: None)

    assert account_path.exists()
