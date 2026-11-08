"""NAS-local catalogue and leased queue for a shared, deduplicated media library."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .library_errors import is_challenge, is_missing_metadata, is_permission_error

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
 source_id TEXT PRIMARY KEY, category TEXT NOT NULL, filename TEXT NOT NULL,
 sha256 TEXT NOT NULL, relative_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_rows(
 source_id TEXT NOT NULL, sheet TEXT NOT NULL, row_number INTEGER NOT NULL,
 work_id TEXT NOT NULL, attributes_json TEXT NOT NULL,
 PRIMARY KEY(source_id,sheet,row_number));
CREATE INDEX IF NOT EXISTS source_work ON source_rows(work_id);
CREATE TABLE IF NOT EXISTS works(
 work_id TEXT PRIMARY KEY, url TEXT NOT NULL, expected_seconds REAL,
 assigned_worker TEXT NOT NULL, priority INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 lease TEXT, lease_worker TEXT, lease_until REAL NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 last_error TEXT, asset_id TEXT, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS work_queue ON works(assigned_worker,status,retry_at,priority);
CREATE INDEX IF NOT EXISTS work_priority ON works(assigned_worker,status,priority);
CREATE INDEX IF NOT EXISTS work_leases ON works(lease_worker,status);
CREATE TABLE IF NOT EXISTS local_artifacts(
 work_id TEXT PRIMARY KEY, worker TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets(
 asset_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, kind TEXT NOT NULL,
 storage_root TEXT NOT NULL, relative_path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 mtime_ns INTEGER NOT NULL, sha256 TEXT, probe_json TEXT NOT NULL,
 created_at REAL NOT NULL, UNIQUE(storage_root,relative_path));
CREATE INDEX IF NOT EXISTS asset_work ON assets(work_id);
CREATE INDEX IF NOT EXISTS asset_hash ON assets(sha256);
CREATE TABLE IF NOT EXISTS candidates(
 work_id TEXT NOT NULL, storage_root TEXT NOT NULL, relative_path TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(storage_root,relative_path));
CREATE INDEX IF NOT EXISTS candidate_work ON candidates(work_id);
CREATE TABLE IF NOT EXISTS user_media_choices(
 work_id TEXT PRIMARY KEY, storage_root TEXT NOT NULL, relative_path TEXT NOT NULL,
 kind TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workers(
 worker TEXT PRIMARY KEY, heartbeat REAL NOT NULL, current_work TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS analysis_cache(
 content_sha256 TEXT NOT NULL, processor TEXT NOT NULL, config_sha256 TEXT NOT NULL,
 status TEXT NOT NULL, result_path TEXT, updated_at REAL NOT NULL,
 PRIMARY KEY(content_sha256,processor,config_sha256));
"""


class LeaseLost(ValueError):
    pass


def probe_media(path: Path, kind: str, expected_seconds=None) -> dict:
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=90, check=True,
    )
    probe = json.loads(result.stdout)
    videos = [s for s in probe.get('streams', []) if s.get('codec_type') == 'video']
    audios = [s for s in probe.get('streams', []) if s.get('codec_type') == 'audio']
    duration = float(probe.get('format', {}).get('duration') or 0)
    if not videos or not duration or not all(s.get('width', 0) > 0 for s in videos):
        raise ValueError('文件缺少有效视频画面或时长')
    if not audios:
        raise ValueError('文件缺少音轨，需复核，不能作为完整素材入库')
    # Huitun rounds source durations; tolerate modest edit/rounding differences,
    # but reject unrelated player captures and truncated downloads.
    if kind == 'video' and expected_seconds and expected_seconds > 0:
        if abs(duration - expected_seconds) > max(5, expected_seconds * .12):
            raise ValueError(f'视频时长与源表不符: {duration:.2f}s / {expected_seconds}s')
    if kind == 'note' and expected_seconds and expected_seconds > 0:
        raise ValueError('源表标有视频时长，但获取了图文，需要人工复核作品类型')
    return {'duration': duration, 'width': videos[0]['width'], 'height': videos[0]['height'],
            'video_codec': videos[0].get('codec_name'), 'audio_codec': audios[0].get('codec_name')}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class LibraryStore:
    def __init__(self, root: Path, *, roots: dict[str, str] | None = None, lease_seconds=300):
        self.root = Path(root).resolve()
        self.roots = {'new': str(self.root), **(roots or {})}
        self.lease_seconds = lease_seconds
        self._publication_lock=threading.Lock()
        for name in ('媒体库', '来源表', '索引', '任务', '分析结果', '临时'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / '索引/library.sqlite3'
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute('PRAGMA journal_mode=WAL')

    def _connection(self):
        db = sqlite3.connect(self.db_path, timeout=60)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def connect(self):
        db = self._connection()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        db = self._connection()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def claim(self, worker: str, *, capacity=1, local_ready=(), local_only=False):
        """Lease at most two tasks per slot; local work bypasses remote cooldown.

        While new remote work exists, reserve at most one in ten remote claims
        for retries. Completed local files and old-library candidates have their
        own lane and do not consume that allowance.
        """
        if worker not in {'mac-1', 'mac-2', 'nas-1', 'nas-2'}:
            raise ValueError('unknown worker')
        if capacity not in (1, 2):
            raise ValueError('capacity must be 1 or 2')
        if len(local_ready) > 128 or any(not str(w).isdigit() for w in local_ready):
            raise ValueError('invalid local ready list')
        host = worker.split('-')[0]
        now = time.time()
        with self.transaction() as db:
            if (self.root/'任务'/'pause-claims').exists():
                return {'task': None, 'wait': 5, 'reason': 'maintenance'}
            db.execute('INSERT INTO workers VALUES(?,?,NULL,?) ON CONFLICT(worker) DO UPDATE SET heartbeat=excluded.heartbeat', (worker, now, 'waiting'))
            db.execute("UPDATE works SET status='retry',retry_at=?,lease=NULL WHERE assigned_worker=? AND status='running' AND lease_until<?", (now, host, now))
            active = db.execute("SELECT count(*) FROM works WHERE lease_worker=? AND status='running'", (worker,)).fetchone()[0]
            if active >= capacity:
                return {'task': None, 'wait': 3, 'reason': 'slot_busy'}
            cooldown = db.execute("SELECT value FROM meta WHERE key=?", ('cooldown_until:'+host,)).fetchone()
            until = float(cooldown[0]) if cooldown else 0
            cooling = until > now
            row = None
            lane = 'local'
            # Recover ready markers written before an API interruption. Preserve
            # ownership so another slot cannot re-download an upload backlog.
            for wid in local_ready:
                db.execute("INSERT OR IGNORE INTO local_artifacts SELECT work_id,lease_worker FROM works WHERE work_id=? AND lease_worker=? AND status IN ('pending','retry','running')", (wid,worker))
            # Ready files belong to this exact scratch slot. They can be retried
            # during cooldown, but still respect transfer-error backoff.
            if local_ready:
                placeholders = ','.join('?' for _ in local_ready)
                row = db.execute(f"SELECT * FROM works WHERE assigned_worker=? AND lease_worker=? AND status IN ('pending','retry') AND retry_at<=? AND work_id IN ({placeholders}) ORDER BY priority LIMIT 1", (host, worker, now, *local_ready)).fetchone()
            if row is None and (cooling or local_only):
                row = db.execute("SELECT w.* FROM works w WHERE w.assigned_worker=? AND w.status IN ('pending','retry') AND w.retry_at<=? AND (EXISTS (SELECT 1 FROM candidates c WHERE c.work_id=w.work_id) OR EXISTS (SELECT 1 FROM user_media_choices u WHERE u.work_id=w.work_id)) AND NOT EXISTS (SELECT 1 FROM local_artifacts l WHERE l.work_id=w.work_id) ORDER BY w.priority LIMIT 1", (host, now)).fetchone()
            if row is None and (cooling or local_only):
                reason = 'host_cooldown' if cooling else 'local_only'
                remaining = max(0, until-now)
                db.execute('UPDATE workers SET message=? WHERE worker=?', (f'{reason}: remaining={remaining:.0f}s',worker))
                return {'task': None, 'wait': min(60,remaining) if cooling else 30,
                        'reason': reason, 'cooldown_until': until, 'remaining_seconds': round(remaining)}
            if row is None:
                lane = 'remote'
                # Smooth the two parsers on one host rather than bursting both
                # immediately when a transfer finishes or cooldown expires.
                gate = db.execute('SELECT value FROM meta WHERE key=?',('next_resolve_at:'+host,)).fetchone()
                if gate and float(gate[0]) > now:
                    return {'task': None, 'wait': float(gate[0])-now, 'reason':'resolve_pacing'}
                counter_key = 'new_since_retry:'+host
                value = db.execute('SELECT value FROM meta WHERE key=?',(counter_key,)).fetchone()
                credit = int(value[0]) if value else 0
                pending = db.execute("SELECT * FROM works w WHERE assigned_worker=? AND status='pending' AND NOT EXISTS (SELECT 1 FROM local_artifacts l WHERE l.work_id=w.work_id) ORDER BY priority LIMIT 1", (host,)).fetchone()
                retry = None
                if pending is None or credit >= 9:
                    retry = db.execute("SELECT * FROM works w WHERE assigned_worker=? AND status='retry' AND retry_at<=? AND NOT EXISTS (SELECT 1 FROM local_artifacts l WHERE l.work_id=w.work_id) ORDER BY priority LIMIT 1", (host,now)).fetchone()
                row = retry if retry is not None else pending
                if row is not None:
                    if db.execute('SELECT 1 FROM candidates WHERE work_id=? UNION ALL SELECT 1 FROM user_media_choices WHERE work_id=? LIMIT 1',(row['work_id'],row['work_id'])).fetchone():
                        lane = 'local'
                    else:
                        db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                   (counter_key,str(0 if retry is not None else min(9,credit+1))))
                    # Only pipelined workers opt into the extra parser pacing.
                    if capacity > 1 and lane == 'remote':
                        db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                   ('next_resolve_at:'+host,str(now+3)))
            if row is None:
                return {'task': None, 'wait': 30, 'reason': 'no_ready_tasks'}
            lease = uuid.uuid4().hex
            db.execute("UPDATE works SET status='running',lease=?,lease_worker=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE work_id=?", (lease, worker, now+self.lease_seconds, now, row['work_id']))
            db.execute('UPDATE workers SET current_work=?,message=? WHERE worker=?', (row['work_id'], 'running '+lane, worker))
            task = dict(row)
            task.update(lease=lease, attempts=row['attempts']+1, lane=lane)
            task['has_candidates'] = bool(db.execute('SELECT 1 FROM candidates WHERE work_id=? UNION ALL SELECT 1 FROM user_media_choices WHERE work_id=? LIMIT 1', (row['work_id'], row['work_id'])).fetchone())
            return {'task': task, 'cooldown_until': until}

    def _check(self, db, work_id, worker, lease):
        row = db.execute('SELECT * FROM works WHERE work_id=?', (work_id,)).fetchone()
        if not row or row['lease_worker'] != worker or row['lease'] != lease or row['status'] != 'running' or row['lease_until'] < time.time():
            raise LeaseLost('任务租约已失效，拒绝提交')
        return row

    def heartbeat(self, work_id, worker, lease):
        with self.transaction() as db:
            self._check(db, work_id, worker, lease)
            now = time.time()
            db.execute('UPDATE works SET lease_until=?,updated_at=? WHERE work_id=?', (now+self.lease_seconds, now, work_id))
            db.execute('UPDATE workers SET heartbeat=?,current_work=? WHERE worker=?', (now, work_id, worker))

    def fail(self, work_id, worker, lease, error, *, phase="resolve", has_local_file=None):
        with self.transaction() as db:
            row = self._check(db, work_id, worker, lease)
            if has_local_file is False:
                db.execute('DELETE FROM local_artifacts WHERE work_id=? AND worker=?',(work_id,worker))
            now = time.time()
            challenged = phase == 'resolve' and is_challenge(error)
            host = worker.split('-')[0]
            missing_metadata = phase == 'resolve' and is_missing_metadata(error)
            failure_key = 'metadata_failures:'+host
            previous = db.execute('SELECT value FROM meta WHERE key=?',(failure_key,)).fetchone()
            failures = (int(previous[0]) if previous else 0) + 1 if missing_metadata else 0
            if phase == 'resolve':
                db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (failure_key,str(failures)))
            delay = min(7200, 180 * 3 ** min(row['attempts']-1, 4))
            # Missing responses are an infrastructure/session failure, not
            # evidence that this particular work is unavailable.
            if missing_metadata:
                delay = max(delay,300)
                db.execute('UPDATE works SET attempts=max(0,attempts-1) WHERE work_id=?',(work_id,))
            if is_permission_error(error):
                delay = max(delay, 6 * 3600)
            status = 'needs_review' if row['attempts'] >= 5 and not missing_metadata else 'retry'
            db.execute('UPDATE works SET status=?,retry_at=?,last_error=?,lease=NULL,updated_at=? WHERE work_id=?', (status, now+delay, error[-3000:], now, work_id))
            if challenged:
                db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=max(CAST(meta.value AS REAL),CAST(excluded.value AS REAL))", ('cooldown_until:'+worker.split('-')[0],str(now+min(delay,900))))
            if missing_metadata and failures >= 3:
                cooldown = min(900,300 * 2 ** min(failures-3,2))
                db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=max(CAST(meta.value AS REAL),CAST(excluded.value AS REAL))',
                           ('cooldown_until:'+host,str(now+cooldown)))
            db.execute('UPDATE workers SET message=?,heartbeat=? WHERE worker=?', (error[-500:], now, worker))
            return {'ok': True, 'reset_browser': challenged or (missing_metadata and failures >= 3)}

    def resolved(self, work_id, worker, lease):
        with self.transaction() as db:
            self._check(db, work_id, worker, lease)
            db.execute('DELETE FROM meta WHERE key=?', ('metadata_failures:'+worker.split('-')[0],))
        return {'ok': True}

    def local_ready(self, work_id, worker, lease):
        with self.transaction() as db:
            self._check(db, work_id, worker, lease)
            db.execute('INSERT INTO local_artifacts VALUES(?,?) ON CONFLICT(work_id) DO UPDATE SET worker=excluded.worker', (work_id,worker))
        return {'ok': True}

    def resolve(self, storage_root, relative_path):
        base = Path(self.roots[storage_root]).resolve()
        path = (base / relative_path).resolve()
        if not path.is_relative_to(base):
            raise ValueError('路径越界')
        return path

    def register(self, work_id, worker, lease, path, kind, probe, sha256, storage_root='new'):
        base = Path(self.roots[storage_root]).resolve()
        relative = str(path.resolve().relative_to(base))
        stat = path.stat()
        with self.transaction() as db:
            self._check(db, work_id, worker, lease)
            existing=db.execute('SELECT asset_id FROM assets WHERE storage_root=? AND relative_path=?',(storage_root,relative)).fetchone()
            asset_id = existing[0] if existing else hashlib.sha256((storage_root+'\0'+relative).encode()).hexdigest()
            db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(storage_root,relative_path) DO UPDATE SET size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,sha256=excluded.sha256,probe_json=excluded.probe_json',
                       (asset_id, work_id, kind, storage_root, relative, stat.st_size, stat.st_mtime_ns, sha256, json.dumps(probe), time.time()))
            db.execute("UPDATE works SET status='completed',asset_id=?,last_error=NULL,updated_at=? WHERE work_id=?", (asset_id,time.time(),work_id))
            db.execute('DELETE FROM local_artifacts WHERE work_id=?',(work_id,))
            db.execute('UPDATE workers SET message=?,heartbeat=? WHERE worker=?', ('completed '+work_id,time.time(),worker))
            # Local reuse/uploads do not prove that remote parsing recovered.
        return {'work_id':work_id, 'asset_id':asset_id, 'storage_root':storage_root, 'relative_path':relative, 'sha256':sha256, 'size_bytes':stat.st_size}

    def reuse(self, work_id, worker, lease):
        with self.connect() as db:
            work = self._check(db, work_id, worker, lease)
            candidates = db.execute('SELECT * FROM candidates WHERE work_id=? ORDER BY size_bytes DESC', (work_id,)).fetchall()
            choice = db.execute('SELECT * FROM user_media_choices WHERE work_id=?', (work_id,)).fetchone()
        if choice:
            # A recorded, content-addressed user choice takes precedence over
            # source-table duration/type hints, but never over media validation.
            try:
                if choice['kind'] not in {'video', 'note'} or not isinstance(choice['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', choice['sha256']):
                    raise ValueError('用户选定的媒体记录无效')
                path = self.resolve(choice['storage_root'], choice['relative_path'])
                before = path.stat()
                if not path.is_file() or before.st_size < 1024:
                    raise ValueError('用户选定的媒体文件不可用')
                probe = probe_media(path, choice['kind'], None)
                digest = file_hash(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or digest != choice['sha256']:
                    raise ValueError('用户选定的媒体文件已改变')
                return self.register(work_id, worker, lease, path, choice['kind'], probe, digest, choice['storage_root'])
            except LeaseLost:
                raise
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                raise ValueError('用户选定的旧库媒体不可用，暂缓下载，待复核') from exc
        for c in candidates:
            try:
                # Zero-duration Huitun rows are predominantly image posts.
                # Historical player captures mislabeled as 视频 must not outrank
                # a verified 图文 rendering just because they are much larger.
                if work['expected_seconds']==0 and c['kind']!='note':continue
                path = self.resolve(c['storage_root'],c['relative_path'])
                if not path.is_file() or path.stat().st_size < 1024:
                    continue
                before = path.stat()
                probe = probe_media(path,c['kind'],work['expected_seconds'])
                digest = file_hash(path)
                after = path.stat()
                if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                    continue
                return self.register(work_id,worker,lease,path,c['kind'],probe,digest,c['storage_root'])
            except LeaseLost:
                raise
            except (OSError,ValueError,subprocess.SubprocessError):
                continue
        # Missing/moved paths must not silently lead to a new download.
        if candidates:
            raise ValueError('旧库存在该作品但未找到通过校验的可用位置，暂缓下载，待旧库路径复核')
        return None

    def publish(self, work_id, worker, lease, staged: Path, kind, digest):
        if kind not in {'video','note'} or not re.fullmatch(r'[a-f0-9]{64}',digest):
            raise ValueError('invalid media metadata')
        with self.connect() as db:
            work = self._check(db,work_id,worker,lease)
        probe = probe_media(staged,kind,work['expected_seconds'])
        with self._publication_lock:
            return self._publish_checked(work_id,worker,lease,staged,kind,digest,probe)

    def _publish_checked(self,work_id,worker,lease,staged,kind,digest,probe):
        # Keep a flat, human-readable library. The catalogue's hash index still
        # reuses identical content without encoding that hash into directories.
        with self.connect() as db:
            duplicate=db.execute("SELECT relative_path FROM assets WHERE storage_root='new' AND sha256=? LIMIT 1",(digest,)).fetchone()
        prefix='图文' if kind=='note' else '视频'
        target=self.resolve('new',duplicate[0]) if duplicate else self.root/'媒体库'/f'{prefix}-{work_id}.mp4'
        if target.exists() and file_hash(target)!=digest:
            target=self.root/'媒体库'/f'{prefix}-{work_id}-{digest[:12]}.mp4'
        if target.exists():
            if target.stat().st_size != staged.stat().st_size or file_hash(target) != digest:
                raise ValueError('已有内容寻址文件校验失败')
            staged.unlink()
        else:
            # Both directories are on the new volume, so publication is atomic.
            os.replace(staged,target)
        return self.register(work_id,worker,lease,target,kind,probe,digest)

    def status(self):
        with self.connect() as db:
            # All counters must describe the same snapshot while workers publish.
            db.execute('BEGIN')
            counts = [dict(r) for r in db.execute('SELECT assigned_worker AS worker,status,count(*) AS count FROM works GROUP BY assigned_worker,status')]
            completion_counts = {host: {'new_download': 0, 'old_reuse': 0, 'unclassified': 0}
                                 for host in ('mac', 'nas')}
            for row in db.execute("""SELECT w.assigned_worker AS worker,
                    CASE WHEN a.storage_root='new' THEN 'new_download'
                         WHEN a.storage_root IS NOT NULL THEN 'old_reuse'
                         ELSE 'unclassified' END AS origin, count(*) AS count
                    FROM works w LEFT JOIN assets a ON a.asset_id=w.asset_id
                    WHERE w.status='completed' GROUP BY w.assigned_worker,origin"""):
                completion_counts.setdefault(row['worker'], {})[row['origin']] = row['count']
            workers = [dict(r) for r in db.execute('SELECT * FROM workers')]
            recent = [dict(r) for r in db.execute("SELECT work_id,assigned_worker,status,last_error,updated_at FROM works WHERE status!='pending' ORDER BY updated_at DESC LIMIT 10")]
            active = [dict(r) for r in db.execute("SELECT work_id,lease_worker,lease_until FROM works WHERE status='running'")]
            now = time.time()
            cooldowns = {r['key'].split(':',1)[1]: max(0,round(float(r['value'])-now))
                         for r in db.execute("SELECT key,value FROM meta WHERE key LIKE 'cooldown_until:%'")}
            space = shutil.disk_usage(self.root)
            return {'counts':counts,'completion_counts':completion_counts,
                    'workers':workers,'recent':recent,'free_bytes':space.free,
                    'active_tasks':active,'cooldown_remaining_seconds':cooldowns,
                    'claims_paused':(self.root/'任务'/'pause-claims').exists(),
                    'scheduler_version':2}

    def locate(self, work_id):
        with self.connect() as db:
            row = db.execute('SELECT w.work_id,w.status,a.* FROM works w LEFT JOIN assets a ON a.asset_id=w.asset_id WHERE w.work_id=?',(work_id,)).fetchone()
            if not row:return None
            result=dict(row)
            if result.get('relative_path'):
                path=self.resolve(result['storage_root'],result['relative_path'])
                result['accessible_path']=str(path)
                result['exists']=path.is_file()
                roots=db.execute("SELECT value FROM meta WHERE key='nas_host_roots'").fetchone()
                if roots:
                    host_roots=json.loads(roots[0])
                    if result['storage_root'] in host_roots:
                        result['nas_path']=str(Path(host_roots[result['storage_root']])/result['relative_path'])
            result['sources']=[dict(r) for r in db.execute('SELECT s.category,s.filename,r.sheet,r.row_number,r.attributes_json FROM source_rows r JOIN sources s USING(source_id) WHERE r.work_id=?',(work_id,))]
            return result
