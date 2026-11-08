"""Resolve on the Playwright owner thread; transfer with an independent client."""
import re
from pathlib import Path


def existing_media(out_dir, wid):
    from .media_catalog import locate_media
    entry = locate_media(out_dir, wid)
    if entry:
        return entry['path']
    for prefix in ('视频', '图文'):
        path = out_dir / f'{prefix}-{wid}.mp4'
        if path.is_file() and path.stat().st_size > 1024:
            return path
    return None


def download_detail(worker, detail, out_dir):
    wid = str(detail['aweme_id'])
    images = detail.get('images')
    note = detail.get('aweme_type') == 68 or isinstance(images, list) and bool(images)
    if note:
        urls = [item['url_list'][-1] for item in (images or [])
                if isinstance(item, dict) and item.get('url_list')]
        bgm = detail.get('music', {}).get('play_url', {}).get('url_list', [])
        path = out_dir / f'图文-{wid}.mp4'
        ok = bool(urls) and worker.synthesize_note_mp4(urls, bgm, path)
    else:
        urls = detail.get('video', {}).get('play_addr', {}).get('url_list', [])
        path = out_dir / f'视频-{wid}.mp4'
        ok = bool(urls) and worker.download_video_stream(urls, path)
    return ok, path, '下载完成' if ok else '元数据已获取，但媒体下载失败'


class MetadataDownloader:
    def __init__(self, storage_state_path, *, browser_recycle_works=25, proxy=None):
        from batch_download_huitun import TableWorker
        self.worker = TableWorker(1, Path(storage_state_path).parent, Path(storage_state_path), proxy,
                                  browser_recycle_works=browser_recycle_works)

    def close(self):
        self.worker.close()

    def resolve(self, url):
        match = re.search(r'(?:video|note)/(\d+)', url)
        if not match:
            raise RuntimeError('无法识别作品ID')
        wid = match[1]
        detail, error = self.worker.resolve_video_info(wid)
        if not detail:
            raise RuntimeError(error or '未获取到作品详情')
        if str(detail.get('aweme_id')) != wid:
            raise RuntimeError('响应作品ID不符，拒绝保存')
        return {'detail': detail, 'user_agent': self.worker.http_client.headers.get('User-Agent', '')}

    def download_url(self, url, out_dir):
        match = re.search(r'(?:video|note)/(\d+)', url)
        if not match:
            return False, out_dir, '无法识别作品ID'
        cached = existing_media(out_dir, match[1])
        if cached:
            return True, cached, '复用本地待入库文件'
        try:
            plan = self.resolve(url)
        except RuntimeError as exc:
            return False, out_dir, str(exc)
        return download_detail(self.worker, plan['detail'], out_dir)


class PreparedDownload:
    """Transfer-only adapter. Never shares Playwright or HTTP clients across threads."""
    def __init__(self, task, plan, scratch):
        self.task = task
        self.plan = plan
        self.scratch = scratch

    def get(self):
        return self

    def download_url(self, url, out_dir):
        cached = existing_media(out_dir, self.task['work_id'])
        if cached:
            return True, cached, '复用本地待入库文件'
        if self.plan is None:
            raise RuntimeError('本地任务缺少完整媒体文件，暂缓下载')
        from batch_download_huitun import TableWorker
        worker = TableWorker(1, out_dir, self.scratch/'transfer-state-unused.json', None)
        try:
            if self.plan.get('user_agent'):
                worker.http_client.headers['User-Agent'] = self.plan['user_agent']
            return download_detail(worker, self.plan['detail'], out_dir)
        finally:
            worker.close()
