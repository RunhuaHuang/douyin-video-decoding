"""Read the requested work from Douyin's server-rendered page data."""
import json
from urllib.parse import unquote, urlsplit


def _http_urls(values):
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if not isinstance(value, str):
            continue
        parsed = urlsplit(value)
        if parsed.scheme in ('https', 'http') and parsed.hostname and value not in result:
            result.append(value)
    return result


def rendered_video_detail(raw, work_id):
    """Normalize the known videoDetail schema; never pick a recommendation."""
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(unquote(raw))
        detail = data['app']['videoDetail']
        if not isinstance(detail, dict) or str(detail.get('awemeId')) != str(work_id):
            return None
        status = detail.get('status') or {}
        if (status.get('isDelete') or status.get('isProhibited')
                or status.get('privateStatus') or detail.get('isPrivate')
                or detail.get('isFriendLimit')):
            return None
        result = {'aweme_id': str(work_id), 'aweme_type': detail.get('awemeType', 0),
                  'desc': detail.get('desc', '')}
        if detail.get('awemeType') == 68 or detail.get('images'):
            images = detail.get('images')
            if not isinstance(images, list) or not images:
                return None
            normalized = []
            for item in images:
                urls = _http_urls(item.get('urlList')) if isinstance(item, dict) else []
                if not urls:
                    return None  # Never silently drop a slide from an image post.
                normalized.append({'url_list': urls})
            music = detail.get('music') or {}
            play = music.get('playUrl') or {}
            result.update(images=normalized, music={'play_url': {'url_list': _http_urls(play.get('urlList'))}})
            return result
        video = detail['video']
        urls = []
        for item in video.get('playAddr') or []:
            url = item.get('src') if isinstance(item, dict) else None
            if not isinstance(url, str):
                continue
            parsed = urlsplit(url)
            # This downloader expects a complete MP4, not a DASH component.
            if (parsed.scheme in ('https', 'http') and parsed.hostname
                    and 'media-video-' not in parsed.path
                    and 'media-audio-' not in parsed.path and url not in urls):
                urls.append(url)
        if not urls:
            return None
        result['video'] = {'duration': video.get('duration'),
                           'width': video.get('width'), 'height': video.get('height'),
                           'play_addr': {'url_list': urls}}
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
