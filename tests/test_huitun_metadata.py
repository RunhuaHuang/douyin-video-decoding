import json
from urllib.parse import quote
from unittest.mock import MagicMock

import pytest

from app.huitun_metadata import rendered_video_detail
from batch_download_huitun import TableWorker


WORK = '7701327355008060712'
URL = 'https://cdn.example/video.mp4'


def payload(work=WORK):
    return {'app': {'videoDetail': {
        'awemeId': work, 'awemeType': 0, 'desc': '目标作品',
        'status': {'isDelete': False, 'privateStatus': 0},
        'video': {'duration': 1143096, 'width': 1920, 'height': 1080,
                  'playAddr': [{'src': URL}, {'src': URL}]},
    }}}


def test_rendered_detail_preserves_target_and_media_duration():
    detail = rendered_video_detail(quote(json.dumps(payload())), WORK)
    assert detail['aweme_id'] == WORK
    assert detail['video']['duration'] == 1143096
    assert detail['video']['play_addr']['url_list'] == [URL]


def test_recommendation_must_not_replace_missing_target():
    data = payload('9999999999999999999')
    data['recommendations'] = [payload()['app']['videoDetail']]
    assert rendered_video_detail(json.dumps(data), WORK) is None


@pytest.mark.parametrize('raw', ['', 'null', '[]', '{}', '{bad', None,
                                     '{"app":{"videoDetail":null}}'])
def test_invalid_render_data_is_not_a_permanent_deletion(raw):
    assert rendered_video_detail(raw, WORK) is None


@pytest.mark.parametrize('change', [
    {'images': [{'urlList': []}]}, {'awemeType': 68},
    {'status': {'isDelete': True}}, {'status': {'privateStatus': 1}},
    {'isFriendLimit': True}, {'video': {'playAddr': [{'src': 'javascript:bad'}]}},
    {'video': {'playAddr': [{'src': 'https://cdn.example/media-video-avc1/file'}]}},
    {'video': {'playAddr': [{'src': 'https://cdn.example/media-audio-und/file'}]}},
])
def test_unsupported_or_restricted_media_does_not_become_download(change):
    data = payload()
    data['app']['videoDetail'].update(change)
    assert rendered_video_detail(json.dumps(data), WORK) is None


def test_image_post_preserves_every_image_and_background_music():
    data = payload()
    data['app']['videoDetail'].update(awemeType=68,
        images=[{'urlList': ['https://cdn.example/1.jpg']}, {'urlList': ['https://cdn.example/2.jpg']}],
        music={'playUrl': {'urlList': ['https://cdn.example/music.mp3']}})
    detail = rendered_video_detail(json.dumps(data), WORK)
    assert detail['aweme_type'] == 68
    assert detail['images'] == [{'url_list': ['https://cdn.example/1.jpg']}, {'url_list': ['https://cdn.example/2.jpg']}]
    assert detail['music']['play_url']['url_list'] == ['https://cdn.example/music.mp3']
    data['app']['videoDetail']['images'][1]['urlList'] = []
    assert rendered_video_detail(json.dumps(data), WORK) is None


def test_empty_api_falls_back_to_jingxuan_and_closes_pages(tmp_path, monkeypatch):
    worker = TableWorker(1, tmp_path, tmp_path/'guest.json')
    first, fallback = MagicMock(), MagicMock()
    first.locator.return_value.count.return_value = 0
    first.title.return_value = '抖音'
    first.url = f'https://www.douyin.com/video/{WORK}'
    fallback.locator.return_value.count.return_value = 1
    fallback.locator.return_value.text_content.return_value = quote(json.dumps(payload()))
    pages = iter([first, fallback])
    monkeypatch.setattr(worker, 'start_browser', lambda: setattr(worker, 'page', next(pages)))
    monkeypatch.setattr('batch_download_huitun.has_visible_captcha', lambda _: False)
    try:
        detail, error = worker.resolve_video_info(WORK, max_retries=1)
        assert error is None and detail['aweme_id'] == WORK
        assert fallback.goto.call_args.args[0] == f'https://www.douyin.com/jingxuan?modal_id={WORK}'
        first.close.assert_called_once()
        fallback.close.assert_called_once()
        fallback.remove_listener.assert_called_once()
        assert worker.page is None
    finally:
        worker.close()
