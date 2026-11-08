"""Shared error classification for library scheduling and browser recovery."""


def is_challenge(error: str) -> bool:
    return any(word in error.lower() for word in
               ('验证码', 'captcha', 'challenge', '风控', '挑战', '反爬'))


def is_missing_metadata(error: str) -> bool:
    return error == '未捕获到视频元数据'


def is_permission_error(error: str) -> bool:
    return '访问受限' in error or '作品已删除' in error


def is_browser_error(error: str) -> bool:
    return any(word in error.lower() for word in
               ('target closed', 'browser closed', 'browser has been closed',
                'target page, context or browser', 'browser disconnected',
                '浏览器异常', 'connection closed'))
