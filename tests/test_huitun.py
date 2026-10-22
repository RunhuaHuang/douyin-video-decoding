import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pandas as pd
import pytest

import batch_download_huitun as batch
import login_douyin
import verify_douyin_interactive
from app import huitun_support as support
from app import huitun_session as session


VIDEO_ID = "7667889682693290106"
DETAIL = {"aweme_type": 0, "video": {"play_addr": {"url_list": ["https://cdn.example/video.mp4"]}}}


def make_table(path, ids=(VIDEO_ID,)):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"播放地址": [f"https://www.douyin.com/video/{item}" for item in ids]}).to_excel(path, index=False)
    return path


@pytest.fixture
def worker(tmp_path, monkeypatch):
    instance = batch.TableWorker(1, tmp_path / "中文 space's 输出", tmp_path / "会话/state.json")
    monkeypatch.setattr(batch.time, "sleep", lambda _: None)
    yield instance
    instance.close()


def install_download(worker, monkeypatch):
    downloaded = []
    def download(urls, target):
        downloaded.append(target)
        target.write_bytes(b"x" * 11000)
        return True
    monkeypatch.setattr(worker, "download_video_stream", download)
    return downloaded


def test_platform_paths_and_user_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "PROJECT_ROOT", tmp_path)
    assert support.default_output_dir("darwin", tmp_path) == tmp_path / "Movies/抖音视频下载"
    assert support.default_state_file("darwin") == tmp_path / "tmp/browser/huitun_state.json"
    assert support.default_output_dir("win32").as_posix() == "D:/抖音视频下载"
    assert support.default_state_file("win32").as_posix() == "D:/抖音视频下载/douyin_state.json"
    assert "Macintosh" in support.browser_user_agent(platform="darwin")
    assert "Windows NT" in support.browser_user_agent(platform="win32")


def test_account_pool_is_copied_without_changing_original(tmp_path, monkeypatch):
    original = tmp_path / "account.json"
    state = {"cookies": [{"name": "sessionid", "value": "fixture"}], "origins": []}
    original.write_text(json.dumps(state), encoding="utf-8")
    account = SimpleNamespace(label="测试账号", account_id="test", storage_state_path=original)
    manager = SimpleNamespace(list_available_accounts=lambda: [account], select_current_or_first_available_account=lambda: account)
    monkeypatch.setattr(support, "CookieAccountManager", lambda: manager)
    target = support.prepare_state_file(tmp_path / "独立/state.json", "测试账号")
    assert json.loads(target.read_text()) == state
    support.save_state(target, {"cookies": [{"name": "sessionid", "value": "new"}], "origins": []})
    assert json.loads(original.read_text()) == state


def test_anonymous_state_never_seeds_from_account_pool(tmp_path, monkeypatch):
    path = tmp_path / "guest.json"
    payload = '{"cookies": [], "origins": []}'
    path.write_text(payload)
    monkeypatch.setattr(support, "CookieAccountManager", lambda: pytest.fail("guest must not load accounts"))
    assert support.prepare_anonymous_state_file(path) == path.resolve()
    assert path.read_text() == payload


@pytest.mark.parametrize("state", [
    {}, {"cookies": [], "origins": None}, {"cookies": ["bad"], "origins": []},
    {"cookies": [{"name": "sessionid", "value": "private-fixture"}], "origins": []},
])
def test_anonymous_state_rejects_invalid_or_authenticated_state(tmp_path, state):
    path = tmp_path / "guest.json"
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError) as exc:
        support.prepare_anonymous_state_file(path)
    assert "private-fixture" not in str(exc.value)


def test_guest_cli_bypasses_pool_and_passes_selected_state(tmp_path, monkeypatch):
    source = make_table(tmp_path / "访客.xlsx")
    state = tmp_path / "guest.json"
    state.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setattr(batch.shutil, "which", lambda _: "/fixture/ffmpeg")
    monkeypatch.setattr(batch, "prepare_state_file", lambda *args: pytest.fail("must not seed old cookies"))
    calls = []
    def run_worker(worker_id, task_queue, dest_dir, limit, state_file, proxy, results, output_name):
        calls.append(state_file)
        results.put(0)
    monkeypatch.setattr(batch, "worker_thread_loop", run_worker)
    monkeypatch.setattr(batch.time, "sleep", lambda _: None)
    assert batch.main([str(source), "-o", str(tmp_path / "out"), "--anonymous-state", str(state), "--proxy", "none"]) == 0
    assert calls == [state.resolve()]


def test_guest_and_account_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        batch.main(["--anonymous-state", "guest.json", "--account", "account"])
    assert exc.value.code == 2


def test_dry_run_uses_user_paths_without_session_or_download(tmp_path, monkeypatch, capsys):
    source = make_table(tmp_path / "中文 space's 输入/榜单.xlsx")
    make_table(source.parent / "~$榜单.xlsx")
    output = tmp_path / "任意输出"
    monkeypatch.setattr(batch, "prepare_state_file", lambda *args: pytest.fail("dry-run must not load sessions"))
    assert batch.main(["--source", str(source.parent), str(source), "-o", str(output), "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "共发现 1 个" in text and str(output / "榜单") in text
    assert not output.exists()


@pytest.mark.parametrize("arguments", [["--concurrency", "0"], ["--test", "-1"]])
def test_cli_rejects_invalid_limits(arguments):
    with pytest.raises(SystemExit) as exc:
        batch.main(arguments)
    assert exc.value.code == 2


def test_cli_rejects_colliding_table_names(tmp_path):
    paths = [make_table(tmp_path / directory / "榜单.xlsx") for directory in ["a", "b"]]
    with pytest.raises(SystemExit) as exc:
        batch.main([*(str(path) for path in paths), "--dry-run"])
    assert exc.value.code == 2


def test_custom_output_name_matches_preview_and_resumes(worker, tmp_path, monkeypatch, capsys):
    table = make_table(tmp_path / "点赞星图总榜.xlsx")
    worker.output_name = "点赞总榜-星图"
    assert batch.main([str(table), "-o", str(worker.dest_dir), "--output-name", worker.output_name, "--dry-run"]) == 0
    assert str(worker.dest_dir / worker.output_name) in capsys.readouterr().out
    monkeypatch.setattr(worker, "resolve_video_info", lambda _: (DETAIL, None))
    downloaded = install_download(worker, monkeypatch)
    assert worker.process_excel(table) == 0
    assert worker.process_excel(table) == 0
    assert downloaded == [worker.dest_dir / worker.output_name / f"视频-{VIDEO_ID}.mp4"]


@pytest.mark.parametrize("name", ["../escape", "/absolute", "..", "a/b", ""])
def test_output_name_cannot_escape_root(tmp_path, name):
    table = make_table(tmp_path / "榜单.xlsx")
    with pytest.raises(SystemExit) as exc:
        batch.main([str(table), "--output-name", name, "--dry-run"])
    assert exc.value.code == 2


def test_output_name_rejects_multiple_tables(tmp_path):
    files = [make_table(tmp_path / name) for name in ("a.xlsx", "b.xlsx")]
    with pytest.raises(SystemExit) as exc:
        batch.main([*(str(path) for path in files), "--output-name", "same", "--dry-run"])
    assert exc.value.code == 2


def test_captcha_recovery_downloads_current_row_and_closes_browser(worker, tmp_path, monkeypatch):
    table = make_table(tmp_path / "恢复.xlsx")
    replies = iter([(None, "触发验证码风控"), (DETAIL, None)])
    monkeypatch.setattr(worker, "resolve_video_info", lambda _: next(replies))
    closed = []
    monkeypatch.setattr(worker, "close_browser", lambda: closed.append(True))
    def cooldown(tag, state_file, duration):
        assert closed and state_file == worker.state_file and duration == 180
        return True
    monkeypatch.setattr(batch.WafCoordinator, "trigger_cooldown", cooldown)
    downloaded = install_download(worker, monkeypatch)
    assert worker.process_excel(table) == 0
    assert len(downloaded) == 1
    assert (worker.dest_dir / "恢复/_COMPLETED.flag").exists()


def test_failed_rows_retry_despite_legacy_completion_marker(worker, tmp_path, monkeypatch):
    table = make_table(tmp_path / "重试.xlsx")
    folder = worker.dest_dir / "重试"
    folder.mkdir(parents=True)
    (folder / "_COMPLETED.flag").touch()
    monkeypatch.setattr(worker, "resolve_video_info", lambda _: (None, "未捕获到元数据"))
    assert worker.process_excel(table) == 1
    assert not (folder / "_COMPLETED.flag").exists()
    monkeypatch.setattr(worker, "resolve_video_info", lambda _: (DETAIL, None))
    install_download(worker, monkeypatch)
    assert worker.process_excel(table) == 0
    assert json.loads((folder / "manifest.json").read_text())[VIDEO_ID]["status"] == "success"


def test_partial_trial_resumes_remaining_rows(worker, tmp_path, monkeypatch):
    table = make_table(tmp_path / "试跑.xlsx", (VIDEO_ID, "7667889682693290107"))
    monkeypatch.setattr(worker, "resolve_video_info", lambda _: (DETAIL, None))
    downloaded = install_download(worker, monkeypatch)
    assert worker.process_excel(table, max_limit=1) == 0
    assert not (worker.dest_dir / "试跑/_COMPLETED.flag").exists()
    assert worker.process_excel(table) == 0
    assert len(downloaded) == 2
    assert (worker.dest_dir / "试跑/_COMPLETED.flag").exists()


def test_metadata_capture_closes_page(worker, monkeypatch):
    callback = {}
    page = MagicMock()
    page.on.side_effect = lambda event, fn: callback.update(response=fn)
    page.goto.side_effect = lambda *args, **kwargs: callback["response"](SimpleNamespace(
        url="https://www.douyin.com/aweme/v1/web/aweme/detail", json=lambda: {"aweme_detail": DETAIL}))
    monkeypatch.setattr(worker, "start_browser", lambda: setattr(worker, "page", page))
    assert worker.resolve_video_info(VIDEO_ID) == (DETAIL, None)
    page.close.assert_called_once()
    assert worker.page is None


@pytest.mark.parametrize("notice,permanent", [
    ("该作品已删除", True), ("该视频不存在", True), ("作品已下架", True),
    ("你已拉黑对方，无法查看其作品", False),
    ("因作品权限或已被删除，无法观看", False),
    ("作品仅自己可见", False), ("暂时无法观看", False),
])
def test_detail_notice_does_not_treat_account_restrictions_as_deletion(worker, monkeypatch, notice, permanent):
    callback = {}
    page = MagicMock()
    page.on.side_effect = lambda event, fn: callback.update(response=fn)
    page.goto.side_effect = lambda *args, **kwargs: callback["response"](SimpleNamespace(
        url="https://www.douyin.com/aweme/v1/web/aweme/detail", json=lambda: {"filter_detail": {"detail_msg": notice}}))
    monkeypatch.setattr(worker, "start_browser", lambda: setattr(worker, "page", page))
    detail, error = worker.resolve_video_info(VIDEO_ID)
    assert detail is None
    assert error.startswith("已删除/不可见(" if permanent else "访问受限(")
    assert batch.is_permanent_unavailability(error) is permanent


@pytest.mark.parametrize("reason", [
    "已删除/不可见(你已拉黑对方，无法查看其作品)",
    "已删除/不可见(因作品权限或已被删除，无法观看)",
    "",
])
def test_legacy_account_restricted_deleted_rows_are_rechecked(worker, tmp_path, monkeypatch, reason):
    table = make_table(tmp_path / "重查.xlsx")
    folder = worker.dest_dir / "重查"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({VIDEO_ID: {"status": "deleted", "reason": reason}}))
    resolver = MagicMock(return_value=(DETAIL, None))
    monkeypatch.setattr(worker, "resolve_video_info", resolver)
    install_download(worker, monkeypatch)
    assert worker.process_excel(table) == 0
    resolver.assert_called_once_with(VIDEO_ID)
    assert json.loads((folder / "manifest.json").read_text())[VIDEO_ID]["status"] == "success"


def test_definite_deletion_still_skips_resolution(worker, tmp_path, monkeypatch):
    table = make_table(tmp_path / "已删除.xlsx")
    folder = worker.dest_dir / "已删除"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({VIDEO_ID: {"status": "deleted", "reason": "已删除/不可见(作品已被删除)"}}))
    monkeypatch.setattr(worker, "resolve_video_info", lambda _: pytest.fail("definite deleted item should be skipped"))
    assert worker.process_excel(table) == 0


def test_access_restriction_is_failed_not_deleted_and_can_retry(worker, tmp_path, monkeypatch):
    table = make_table(tmp_path / "访问受限.xlsx")
    monkeypatch.setattr(worker, "resolve_video_info", lambda _: (None, "访问受限(因作品权限或已被删除，无法观看)"))
    assert worker.process_excel(table) == 1
    manifest = worker.dest_dir / "访问受限/manifest.json"
    assert json.loads(manifest.read_text())[VIDEO_ID]["status"] == "failed"
    monkeypatch.setattr(worker, "resolve_video_info", lambda _: (DETAIL, None))
    install_download(worker, monkeypatch)
    assert worker.process_excel(table) == 0
    assert json.loads(manifest.read_text())[VIDEO_ID]["status"] == "success"


def test_guest_verification_does_not_copy_account_session(tmp_path, monkeypatch):
    path = tmp_path / "guest.json"
    path.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setattr(verify_douyin_interactive, "prepare_state_file", lambda _: pytest.fail("must not load account pool"))
    capture = MagicMock(return_value=0)
    monkeypatch.setattr(verify_douyin_interactive, "capture_session", capture)
    url = f"https://www.douyin.com/video/{VIDEO_ID}"
    assert verify_douyin_interactive.run_interactive_verify([url, "--anonymous-state", str(path), "--proxy", "none"]) == 0
    capture.assert_called_once_with(path.resolve(), None, verify_url=url, timeout=300)


@pytest.mark.parametrize("url,kind,blocked", [
    ("https://v3.douyinvod.com/opaque-path", "fetch", True),
    ("https://example.org/video.mp4?token=x", "xhr", True),
    ("https://example.org/media-audio-und-mp4a/", "fetch", True),
    ("https://example.org/avatar", "image", True),
    ("https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123", "fetch", False),
    ("https://www.douyin.com/app.js", "script", False),
])
def test_browser_media_policy(url, kind, blocked):
    assert support.should_block_browser_request(url, kind) is blocked


def test_login_and_verification_share_custom_state_and_proxy(tmp_path, monkeypatch):
    target = tmp_path / "中文 session.json"
    calls = []
    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return 0
    monkeypatch.setattr(login_douyin, "capture_session", capture)
    monkeypatch.setattr(verify_douyin_interactive, "capture_session", capture)
    monkeypatch.setattr(verify_douyin_interactive, "prepare_state_file", lambda path: path)
    arguments = ["--state-file", str(target), "--proxy", "http://127.0.0.1:7899"]
    assert login_douyin.run_login(arguments) == 0
    assert verify_douyin_interactive.run_interactive_verify([f"https://www.douyin.com/video/{VIDEO_ID}", *arguments]) == 0
    assert calls[0][0] == calls[1][0] == (target, "http://127.0.0.1:7899")


def test_cancelled_login_preserves_existing_session(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("original session", encoding="utf-8")
    playwright = MagicMock()
    browser = playwright.chromium.launch.return_value
    browser.version = "128.0.0.0"
    browser.new_context.return_value.new_page.return_value.is_closed.return_value = True
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    monkeypatch.setattr(session, "sync_playwright", lambda: manager)
    assert session.capture_session(target, None) == 1
    assert target.read_text() == "original session"


def test_verification_survives_navigation_after_captcha(tmp_path, monkeypatch):
    from playwright.sync_api import Error
    target = tmp_path / "state.json"
    target.write_text("original session", encoding="utf-8")
    playwright = MagicMock()
    browser = playwright.chromium.launch.return_value
    browser.version = "128.0.0.0"
    browser.is_connected.return_value = True
    context = browser.new_context.return_value
    state = {"cookies": [{"name": "sessionid", "value": "verified"}], "origins": []}
    context.storage_state.return_value = state
    page = context.new_page.return_value
    page.is_closed.return_value = False
    page.url = f"https://www.douyin.com/video/{VIDEO_ID}"
    page.title.side_effect = [Error("Execution context was destroyed, most likely because of a navigation"), "抖音视频"]
    page.frames = []
    page.locator.return_value.count.return_value = 1
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    monkeypatch.setattr(session, "sync_playwright", lambda: manager)
    assert session.capture_session(target, None, verify_url=page.url) == 0
    assert json.loads(target.read_text()) == state
    page.wait_for_timeout.assert_called_once_with(1000)
    context.storage_state.assert_called_once_with()


@pytest.mark.parametrize("verify", [False, True])
def test_session_wait_does_not_open_storage_tabs(tmp_path, monkeypatch, verify):
    playwright = MagicMock()
    browser = playwright.chromium.launch.return_value
    browser.version = "128.0.0.0"
    browser.is_connected.return_value = True
    context = browser.new_context.return_value
    state = {"cookies": [{"name": "sessionid", "value": "verified"}], "origins": []}
    context.storage_state.return_value = state
    context.cookies.side_effect = [[], [], state["cookies"]]
    page = context.new_page.return_value
    page.is_closed.return_value = False
    page.wait_for_timeout.side_effect = lambda _: context.storage_state.assert_not_called()
    complete = MagicMock(side_effect=[False, False, True])
    monkeypatch.setattr(session, "verification_complete", complete)
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    monkeypatch.setattr(session, "sync_playwright", lambda: manager)
    target = tmp_path / "state.json"
    verify_url = f"https://www.douyin.com/video/{VIDEO_ID}" if verify else None
    assert session.capture_session(target, None, verify_url=verify_url) == 0
    assert page.wait_for_timeout.call_count == 2
    context.storage_state.assert_called_once_with()
    assert json.loads(target.read_text()) == state


@pytest.mark.skipif(os.environ.get("DOUYIN_BROWSER_SMOKE") != "1", reason="requires installed Chromium")
def test_verification_ignores_hidden_challenge_frame():
    with session.sync_playwright() as playwright:
        browser = playwright.chromium.launch(**support.browser_launch_options(None, headless=True))
        try:
            context = browser.new_context()
            page = context.new_page()
            context.route("**/*", lambda route: route.fulfill(
                content_type="text/html", body=(
                    '<title>抖音视频</title><video></video>'
                    '<iframe style="display:none" src="https://verifycenter.invalid/rmc-nocaptcha/"></iframe>'
                    if route.request.url.startswith("https://www.douyin.com/") else "<p>challenge</p>"
                )))
            page.goto(f"https://www.douyin.com/video/{VIDEO_ID}")
            assert len(page.frames) == 2
            assert session.verification_complete(page, False)
            page.locator("iframe").evaluate("frame => frame.style.display = 'block'")
            assert not session.verification_complete(page, True)
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("DOUYIN_BROWSER_SMOKE") != "1", reason="requires installed Chromium")
def test_real_browser_blocks_fetch_media_and_httpx_download_bypasses_proxy(tmp_path, monkeypatch):
    from playwright.sync_api import Page
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"<title>\xe6\x8a\x96\xe9\x9f\xb3 test</title>" if self.path.endswith("/page") else b"x" * 16384
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if self.path.endswith("/page") else "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxy = f"http://127.0.0.1:{server.server_port}"
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        monkeypatch.setenv(key, proxy)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    goto = Page.goto
    def local_home(page, url, **kwargs):
        if url == "https://www.douyin.com/":
            url = "http://huitun-probe.invalid/page"
        return goto(page, url, **kwargs)
    monkeypatch.setattr(Page, "goto", local_home)
    worker = batch.TableWorker(1, tmp_path, tmp_path / "absent.json", proxy)
    try:
        worker.start_browser()
        blocked = worker.page.evaluate("async () => {try {await fetch('/video.mp4'); return false;} catch {return true;}}")
        assert blocked
        assert not any("video.mp4" in path for path in hits)
        assert worker.download_video_stream([proxy + "/file.mp4"], tmp_path / "视频.mp4")
        assert "/file.mp4" in hits  # Direct origin-form request; an HTTP proxy uses absolute-form.
        assert proxy + "/file.mp4" not in hits
        assert (tmp_path / "视频.mp4").stat().st_size == 16384
    finally:
        worker.close()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="requires ffmpeg")
def test_note_synthesis_with_unicode_and_spaces(worker):
    image = subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240", "-frames:v", "1",
        "-f", "image2pipe", "-c:v", "png", "pipe:1",
    ], capture_output=True, check=True, timeout=30).stdout
    worker.http_client.close()
    worker.http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=image)))
    target = worker.dest_dir / "图文.mp4"
    assert worker.synthesize_note_mp4(["https://fixture/1", "https://fixture/2"], [], target)
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(target)],
                            capture_output=True, check=True, timeout=30)
    info = json.loads(result.stdout)
    assert {stream["codec_type"] for stream in info["streams"]} == {"video", "audio"}
    assert abs(float(info["format"]["duration"]) - 2.0) < 0.2
