from pathlib import Path
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

from app.video_downloader import VideoDownloader, detect_url_column, main


def test_detect_url_column():
    df1 = pd.DataFrame({"视频标题": ["t1"], "视频链接": ["https://v.douyin.com/123/"]})
    assert detect_url_column(df1) == "视频链接"

    df2 = pd.DataFrame({"商品": ["p1"], "播放地址": ["https://www.douyin.com/video/1"]})
    assert detect_url_column(df2) == "播放地址"

    df3 = pd.DataFrame({"title": ["t1"], "custom_link": ["https://v.douyin.com/123/"]})
    assert detect_url_column(df3, "custom_link") == "custom_link"

    df4 = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
    with pytest.raises(ValueError, match="未能在表格中自动识别视频链接列"):
        detect_url_column(df4)


def test_video_url_can_be_converted_to_note_route():
    assert (
        VideoDownloader._note_url_for_video("https://www.douyin.com/video/7667890534388359355")
        == "https://www.douyin.com/note/7667890534388359355"
    )
    assert VideoDownloader._note_url_for_video("https://www.douyin.com/note/123") is None
    assert VideoDownloader._is_douyin_challenge_html(
        '<script>byted_acrawler.init(); window.location.reload(); var __ac_signature="x"</script>'
    )
    assert not VideoDownloader._is_douyin_challenge_html("<html><body>normal</body></html>")


def test_challenge_navigation_uses_commit_wait_until(tmp_path):
    downloader = VideoDownloader()
    try:
        page = MagicMock()
        page.goto.return_value = None
        page.wait_for_timeout.return_value = None
        page.content.return_value = "<html>normal</html>"
        page.url = "https://www.douyin.com/video/1"
        page.is_closed.return_value = False
        context = MagicMock()
        context.new_page.return_value = page
        def write_video(**kwargs):
            kwargs["output_path"].write_bytes(b"video" * 512)

        with patch.object(downloader.browser_resolver, "_ensure_context", return_value=context), patch.object(
            downloader.browser_resolver, "download_video", side_effect=write_video
        ):
            downloader.download_url(
                "https://www.douyin.com/video/1",
                out_dir=tmp_path,
                skip_existing=False,
                _is_note_fallback=True,
            )
        page.goto.assert_called_once_with(
            "https://www.douyin.com/video/1",
            wait_until="commit",
            timeout=downloader.browser_resolver.navigation_timeout_ms,
        )
    finally:
        downloader.close()


def test_challenge_page_routes_to_note_after_commit(tmp_path):
    downloader = VideoDownloader()
    try:
        challenge_page = MagicMock()
        challenge_page.content.return_value = (
            '<script>window.location.reload(); var __ac_signature="x"</script>'
        )
        challenge_page.url = "https://www.douyin.com/video/1"
        challenge_page.is_closed.return_value = False

        note_page = MagicMock()
        note_page.content.return_value = "<html>note</html>"
        note_page.url = "https://www.douyin.com/note/1"
        note_page.is_closed.return_value = False
        note_page.wait_for_selector.side_effect = RuntimeError("no images")
        note_page.evaluate.return_value = []

        context = MagicMock()
        context.new_page.side_effect = [challenge_page, note_page]
        with patch.object(downloader.browser_resolver, "_ensure_context", return_value=context):
            ok, _, message = downloader.download_url(
                "https://www.douyin.com/video/1",
                out_dir=tmp_path,
                skip_existing=False,
                _is_note_fallback=True,
            )

        assert ok is False
        assert "note 重试" in message
        assert context.new_page.call_count == 2
        challenge_page.goto.assert_called_once_with(
            "https://www.douyin.com/video/1",
            wait_until="commit",
            timeout=downloader.browser_resolver.navigation_timeout_ms,
        )
        note_page.goto.assert_called_once_with(
            "https://www.douyin.com/note/1",
            wait_until="commit",
            timeout=downloader.browser_resolver.navigation_timeout_ms,
        )
    finally:
        downloader.close()


def test_video_uses_shared_douyin_service_download_chain(tmp_path):
    downloader = VideoDownloader()
    try:
        service = MagicMock()
        service.parse_share_text.return_value = MagicMock(
            video_id="7683110075418310314",
            is_deleted=False,
        )

        def write_video(video, output_path, audio_output_path=None):
            output_path.write_bytes(b"video" * 512)

        service.download_video.side_effect = write_video
        with patch.object(downloader, "_get_douyin_service", return_value=service), patch.object(
            downloader.browser_resolver, "_ensure_context", return_value=MagicMock()
        ), patch.object(downloader.browser_resolver, "download_video") as browser_download:
            ok, path, message = downloader.download_url(
                "https://www.douyin.com/video/7683110075418310314",
                out_dir=tmp_path,
                skip_existing=False,
            )

        assert ok is True
        assert path == tmp_path / "视频-7683110075418310314.mp4"
        assert path.read_bytes() == b"video" * 512
        assert "视频成功" in message
        service.parse_share_text.assert_called_once_with(
            "https://www.douyin.com/video/7683110075418310314"
        )
        service.download_video.assert_called_once()
        browser_download.assert_not_called()
    finally:
        downloader.close()


def test_preflight_challenge_routes_to_note_before_page_navigation(tmp_path, monkeypatch):
    monkeypatch.setattr("app.video_downloader.DOUYIN_PREFLIGHT_CHALLENGE", True)
    downloader = VideoDownloader()
    try:
        note_page = MagicMock()
        note_page.content.return_value = "<html>note</html>"
        note_page.url = "https://www.douyin.com/note/1"
        note_page.is_closed.return_value = False
        note_page.wait_for_selector.side_effect = RuntimeError("no images")
        note_page.evaluate.return_value = []

        context = MagicMock()
        context.request.get.side_effect = [
            MagicMock(text=lambda: '<script>window.location.reload(); var __ac_signature="x"</script>'),
            MagicMock(text=lambda: "<html>note</html>"),
        ]
        context.new_page.return_value = note_page
        with patch.object(downloader.browser_resolver, "_ensure_context", return_value=context):
            ok, _, message = downloader.download_url(
                "https://www.douyin.com/video/1",
                out_dir=tmp_path,
                skip_existing=False,
            )

        assert ok is False
        assert "note 重试" in message
        assert context.new_page.call_count == 1
        note_page.goto.assert_called_once_with(
            "https://www.douyin.com/note/1",
            wait_until="commit",
            timeout=downloader.browser_resolver.navigation_timeout_ms,
        )
    finally:
        downloader.close()


def test_cli_list_accounts(capsys):
    ret = main(["--list-accounts"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "吴倩" in captured.out or "账号池" in captured.out


def test_cli_no_args():
    ret = main([])
    assert ret == 2


def test_skip_existing(tmp_path):
    video_file = tmp_path / "视频-7674160480689018597.mp4"
    video_file.write_bytes(b"0" * 2048)

    downloader = VideoDownloader()
    try:
        ok, path, msg = downloader.download_url(
            "https://www.douyin.com/video/7674160480689018597",
            out_dir=tmp_path,
            skip_existing=True,
        )
        assert ok is True
        assert path == video_file
        assert "已存在，跳过" in msg
    finally:
        downloader.close()


def test_download_url_rejects_non_douyin_url(tmp_path):
    downloader = VideoDownloader()
    try:
        with pytest.raises(ValueError, match="不是有效的抖音链接"):
            downloader.download_url("https://example.com/internal", out_dir=tmp_path)
    finally:
        downloader.close()


def test_cli_table_target_dir_logic(tmp_path):
    table_csv = tmp_path / "测试表格.csv"
    table_csv.write_text("视频链接\nhttps://v.douyin.com/123/\n", encoding="utf-8")

    downloader = VideoDownloader()
    try:
        with patch.object(downloader, "download_url", return_value=(True, tmp_path / "视频-123.mp4", "成功")) as mock_dl:
            succ, fail = downloader.download_file(table_csv, out_dir=tmp_path / "测试表格", skip_existing=True, workers=1)
            assert succ == 1
            assert fail == 0
            assert mock_dl.call_count == 1
    finally:
        downloader.close()


def test_early_skip_without_browser(tmp_path):
    # 当文件已存在时，应毫秒级返回，不应拉起浏览器上下文
    target_file = tmp_path / "视频-7661240833752086186.mp4"
    target_file.write_bytes(b"x" * 2048)

    downloader = VideoDownloader()
    try:
        with patch.object(downloader.browser_resolver, "_ensure_context") as mock_ctx:
            ok, path, msg = downloader.download_url(
                "https://www.douyin.com/video/7661240833752086186",
                out_dir=tmp_path,
                skip_existing=True,
            )
            assert ok is True
            assert path == target_file
            assert "已存在，跳过" in msg
            mock_ctx.assert_not_called()
    finally:
        downloader.close()


def test_recycle_browser_trigger(tmp_path):
    downloader = VideoDownloader(recycle_interval=2)
    try:
        with patch.object(downloader, "recycle_browser", wraps=downloader.recycle_browser) as mock_recycle:
            with patch.object(downloader.browser_resolver, "_ensure_context") as mock_ensure:
                mock_page = MagicMock()
                mock_page.url = "https://www.douyin.com/video/111"
                mock_ensure.return_value.new_page.return_value = mock_page
                with patch.object(downloader.browser_resolver, "download_video") as mock_dl:
                    # 第一次请求
                    downloader.download_url("https://www.douyin.com/video/111", out_dir=tmp_path, skip_existing=False)
                    assert downloader._active_downloads == 1
                    assert mock_recycle.call_count == 0

                    # 第二次请求
                    downloader.download_url("https://www.douyin.com/video/222", out_dir=tmp_path, skip_existing=False)
                    assert downloader._active_downloads == 2
                    assert mock_recycle.call_count == 0

                    # 第三次请求（达到 recycle_interval=2 触发回收）
                    downloader.download_url("https://www.douyin.com/video/333", out_dir=tmp_path, skip_existing=False)
                    assert mock_recycle.call_count == 1
                    assert downloader._active_downloads == 1
    finally:
        downloader.close()


def test_download_file_start_row(tmp_path):
    table_csv = tmp_path / "测试表格.csv"
    table_csv.write_text("视频链接\nhttps://v.douyin.com/1/\nhttps://v.douyin.com/2/\nhttps://v.douyin.com/3/\n", encoding="utf-8")

    downloader = VideoDownloader()
    try:
        with patch.object(downloader, "download_url", return_value=(True, tmp_path / "out.mp4", "成功")) as mock_dl:
            succ, fail = downloader.download_file(table_csv, out_dir=tmp_path, skip_existing=True, start_row=2, workers=1)
            assert succ == 2
            assert fail == 0
            # 应只处理第 2 和第 3 条
            assert mock_dl.call_count == 2
            assert mock_dl.call_args_list[0][0][0] == "https://v.douyin.com/2/"
            assert mock_dl.call_args_list[1][0][0] == "https://v.douyin.com/3/"
    finally:
        downloader.close()


def test_download_file_uses_distinct_cookie_workers(tmp_path):
    table_csv = tmp_path / "并发.csv"
    table_csv.write_text(
        "视频链接\n" + "\n".join(f"https://www.douyin.com/video/{i}" for i in range(1, 5)) + "\n",
        encoding="utf-8",
    )

    class Account:
        def __init__(self, label):
            self.label = label
            self.storage_state_path = tmp_path / f"{label}.json"

    accounts = [Account("账号1"), Account("账号2")]

    class Manager:
        def select_current_or_first_available_account(self):
            return accounts[0]

        def list_available_accounts(self):
            return accounts

    downloader = VideoDownloader(account_manager=Manager())
    try:
        with patch.object(
            VideoDownloader,
            "download_url",
            return_value=(True, tmp_path / "out.mp4", "成功"),
        ) as mock_dl:
            success, failed = downloader.download_file(
                table_csv,
                out_dir=tmp_path / "out",
                workers=2,
            )
        assert (success, failed) == (4, 0)
        assert mock_dl.call_count == 4
    finally:
        downloader.close()


def test_download_file_explicit_accounts(tmp_path):
    table_csv = tmp_path / "指定账号.csv"
    table_csv.write_text("视频链接\nhttps://www.douyin.com/video/1\n", encoding="utf-8")

    class Account:
        def __init__(self, label):
            self.label = label
            self.storage_state_path = tmp_path / f"{label}.json"

    accounts = [Account("吴倩"), Account("思思"), Account("老脏")]

    class Manager:
        def select_current_or_first_available_account(self):
            return accounts[0]

        def list_available_accounts(self):
            return accounts

    downloader = VideoDownloader(account_manager=Manager())
    try:
        with patch.object(
            VideoDownloader,
            "download_url",
            return_value=(True, tmp_path / "out.mp4", "成功"),
        ) as mock_dl:
            success, failed = downloader.download_file(
                table_csv,
                out_dir=tmp_path / "out",
                workers=2,
                accounts=["思思", "老脏"],
            )
        assert (success, failed) == (1, 0)
    finally:
        downloader.close()


def test_download_file_rejects_when_all_explicit_accounts_are_invalid(tmp_path):
    table_csv = tmp_path / "指定账号失败.csv"
    table_csv.write_text("视频链接\nhttps://www.douyin.com/video/1\n", encoding="utf-8")

    class Account:
        label = "可用账号"
        storage_state_path = tmp_path / "可用账号.json"

    class Manager:
        def select_current_or_first_available_account(self):
            return Account()

        def list_available_accounts(self):
            return [Account()]

    downloader = VideoDownloader(account_manager=Manager())
    try:
        with pytest.raises(RuntimeError, match="指定的账号均不可用"):
            downloader.download_file(table_csv, out_dir=tmp_path, accounts=["不存在"])
    finally:
        downloader.close()
