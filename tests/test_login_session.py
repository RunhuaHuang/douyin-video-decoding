from pathlib import Path

from app import login_session


class _DummyPage:
    def __init__(self) -> None:
        self.goto_calls = []

    def goto(self, url: str, wait_until: str = "") -> None:
        self.goto_calls.append((url, wait_until))

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        return None


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

    def close(self) -> None:
        return None


class _DummyBrowser:
    def __init__(self) -> None:
        self.new_context_calls = []
        self.context = _DummyContext()

    def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        return self.context

    def close(self) -> None:
        return None


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
    assert browser.context.storage_state_path == str(account_path)
