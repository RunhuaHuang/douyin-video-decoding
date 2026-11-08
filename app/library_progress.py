"""Read-only terminal progress reporting, independent of download threads."""
from datetime import datetime
import logging
import threading
import time


LOG = logging.getLogger('library-progress')


def summarize(status):
    counts = {'mac': {}, 'nas': {}}
    for item in status.get('counts', []):
        counts.setdefault(item['worker'], {})[item['status']] = item['count']
    return counts


def format_progress(status, previous=None, elapsed=None):
    counts = summarize(status)
    total = sum(sum(values.values()) for values in counts.values())
    complete = sum(values.get('completed', 0) for values in counts.values())
    ratio = complete / total if total else 0
    units = min(24 * 8, int(ratio * 24 * 8))
    filled, fraction = divmod(units, 8)
    partial = ['', '▏', '▎', '▍', '▌', '▋', '▊', '▉'][fraction]
    bar = '█' * filled + partial + '░' * (24 - filled - bool(partial))
    lines = [
        f"========== 下载进度 {datetime.now().astimezone():%H:%M:%S} ==========",
        f"总体 [{bar}] {ratio:.2%}  已入库 {complete:,} / {total:,}",
    ]
    for host, label in [('mac', 'Mac'), ('nas', 'NAS')]:
        values = counts[host]
        target = sum(values.values())
        done = values.get('completed', 0)
        percent = done / target if target else 0
        cooldown = int(status.get('cooldown_remaining_seconds', {}).get(host, 0))
        if status.get('claims_paused'):
            state = '维护暂停领取'
        elif cooldown > 0:
            state = f'远程冷却 {cooldown//60:02}:{cooldown%60:02}（本地继续）'
        else:
            state = '暂无任务' if not target else ('运行中' if done < target else '已完成')
        stale = sum(time.time() - worker['heartbeat'] > 120
                    for worker in status.get('workers', [])
                    if worker['worker'].startswith(host+'-'))
        if stale:
            state += f'；{stale}个工作槽心跳过期'
        lines.append(
            f"{label} {percent:6.2%}  {done:>7,} / {target:,} | "
            f"{state}"
        )
        origins = status.get('completion_counts', {}).get(host)
        before = (previous or {}).get('completion_counts', {}).get(host)
        if origins is None:
            lines.append('    新下载 — | 旧库复用 — | 等待服务提供分类统计')
        else:
            lines.append(f"    累计新下载 {origins['new_download']:,} | 旧库复用 {origins['old_reuse']:,}"
                         + (f" | 来源待核对 {origins['unclassified']:,}" if origins.get('unclassified') else ''))
            if before is not None and elapsed and elapsed > 0:
                downloaded = origins['new_download'] - before['new_download']
                reused = origins['old_reuse'] - before['old_reuse']
                if downloaded >= 0 and reused >= 0:
                    lines.append(f'    近{max(1, round(elapsed))}秒：新下载 +{downloaded:,} | '
                                 f'旧库复用 +{reused:,} | 下载速度 {downloaded * 60 / elapsed:.1f} 条/分钟')
                else:
                    lines.append('    统计基线已变更，下一轮重新计算速度')
            else:
                lines.append('    本轮新下载 — | 旧库复用 — | 下载速度 —（等待下一轮采样）')
        lines.append(f"    待处理 {values.get('pending', 0):,} | 在途 {values.get('running', 0)} | "
                     f"待重试 {values.get('retry', 0):,} | 待复核 {values.get('needs_review', 0):,}")
    lines.append('口径：新下载仅计成功入库作品；速度为采样区间新下载条数，不含旧库复用。')
    lines.append('待重试、待复核及传输中的文件不计完成；条/分钟随视频大小波动。')
    lines.append('=' * 58)
    return '\n'.join(lines)


class ProgressReporter:
    def __init__(self, client_factory, interval=30):
        self.client_factory = client_factory
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='terminal-progress', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=12)

    def run(self):
        client = self.client_factory()
        previous = None
        previous_time = None
        try:
            while not self.stop_event.is_set():
                try:
                    status = client.request('GET', '/status', timeout=10)
                    now = time.monotonic()
                    LOG.info('\n%s', format_progress(status, previous,
                             now-previous_time if previous_time is not None else None))
                    previous = status
                    previous_time = now
                except Exception as exc:
                    LOG.warning('进度暂时无法读取，将自动重试：%s', exc)
                self.stop_event.wait(self.interval)
        finally:
            client.close()
