import json
from pathlib import Path

from app import huitun_resolver as module


def test_guest_reset_preserves_source_and_isolates_slots(tmp_path, monkeypatch):
    created = []
    class Resolver:
        def __init__(self, state, **kwargs):
            self.state = state
            self.closed = False
            created.append(self)
        def resolve(self, url):
            return {'detail': {'aweme_id': url.rsplit('/', 1)[-1]}}
        def close(self):
            self.closed = True
    monkeypatch.setattr(module, 'IsolatedMetadataResolver', Resolver)
    source = tmp_path / 'original.json'
    original = '{"cookies": [], "origins": [], "keep": true}'
    source.write_text(original)
    a = module.TableResolver(source, anonymous=True, reset_seconds=1800)
    b = module.TableResolver(source, anonymous=True)
    try:
        a.resolve('https://www.douyin.com/video/123')
        b.resolve('https://www.douyin.com/video/123')
        assert created[0].state != created[1].state != source
        old = created[0].state
        a.last_reset -= 1801
        a.resolve('https://www.douyin.com/video/456')
        assert created[0].closed and not old.exists()
        assert source.read_text() == original
        assert json.loads(created[-1].state.read_text()) == {'cookies': [], 'origins': []}
    finally:
        a.close()
        b.close()


def test_batch_reports_timeout_and_next_item_can_resolve(tmp_path):
    from batch_download_huitun import TableWorker
    class Resolver:
        calls = 0
        def resolve(self, url):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('浏览器异常：解析超过90秒，已回收解析进程')
            return {'detail': {'aweme_id': '456'}, 'user_agent': 'fixture'}
        def close(self):
            pass
    worker = TableWorker(1, tmp_path)
    worker.isolated_resolver = Resolver()
    try:
        assert '90秒' in worker.resolve_table_detail('123')[1]
        assert worker.resolve_table_detail('456') == ({'aweme_id': '456'}, None)
        assert worker.http_client.headers['User-Agent'] == 'fixture'
    finally:
        worker.close()
