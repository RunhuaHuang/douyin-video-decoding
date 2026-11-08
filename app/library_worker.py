"""One independent browser worker; NAS and Mac each run two instances."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import ssl
import threading
import time
from pathlib import Path

import httpx
import portalocker

from .library_store import file_hash, probe_media
from .library_errors import is_browser_error, is_challenge, is_missing_metadata
from .library_downloader import PreparedDownload, existing_media

LOG=logging.getLogger('library-worker')


class BrowserSession:
    def __init__(self, path: Path, seed: Path | None = None, *, reset_seconds=1800, factory=None):
        self.path=path;self.reset_seconds=reset_seconds;self.factory=factory
        self.engine=None;self.started=0.;self.last_reset=time.monotonic()
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():
            if seed:shutil.copyfile(seed,path)
            else:path.write_text(json.dumps({'cookies':[],'origins':[]}))
            path.chmod(0o600)

    def get(self):
        if time.monotonic()-self.last_reset >= self.reset_seconds:
            self.reset()
        if self.engine is None:
            if self.factory is None:
                from .video_downloader import VideoDownloader
                self.factory=VideoDownloader
            self.engine=self.factory(storage_state_path=self.path)
            self.started=time.monotonic()
        return self.engine

    def reset(self):
        if self.engine is not None:
            self.engine.close()
        self.engine=None
        self.last_reset=time.monotonic()
        # A closed Playwright browser discards its ephemeral profile/cache.
        # Do NOT reseed old cookies: the user explicitly requested a clean
        # working session every 30 minutes. Original account files stay intact.
        self.path.write_text(json.dumps({'cookies':[],'origins':[]}))
        self.path.chmod(0o600)
        LOG.info('已清空工作 cookies、关闭浏览器并释放临时缓存（账号池未修改）')

    def close(self):
        if self.engine:self.engine.close()


def maintain_scratch(client,scratch,active=()):
    """Run only between tasks while holding this worker's process lock."""
    retained=0
    for folder in scratch.glob('work-*'):
        if not folder.is_dir() or folder.name.removeprefix('work-') in active:continue
        if not (folder/'ready.json').exists():
            shutil.rmtree(folder);continue
        try:
            state=client.request('GET','/works/'+folder.name.removeprefix('work-'))
            if state.get('status')=='completed' and state.get('exists'):
                shutil.rmtree(folder);continue
        except Exception:
            pass
        retained+=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
    return retained


class Client:
    def __init__(self,url,token,ca_file=None):
        verify=ssl.create_default_context(cafile=str(ca_file)) if ca_file else True
        self.http=httpx.Client(base_url=url,headers={'Authorization':'Bearer '+token},trust_env=False,verify=verify,timeout=httpx.Timeout(7200,connect=15))

    def request(self,method,path,**kwargs):
        if not path.startswith(('/commit/', '/upload/', '/reuse/')):
            kwargs.setdefault('timeout',httpx.Timeout(30,connect=15))
        result=self.http.request(method,path,**kwargs)
        if result.is_error:
            raise RuntimeError(f'API {result.status_code}: {result.text[:1000]}')
        return result.json()

    def close(self):self.http.close()


class Heartbeat:
    def __init__(self,client,work_id,worker,lease):
        self.client=client;self.path=f'/heartbeat/{work_id}/{worker}/{lease}'
        self.stop=threading.Event();self.lost=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True)

    def run(self):
        while not self.stop.wait(30):
            try:self.client.request('POST',self.path)
            except Exception as exc:
                LOG.warning('续租暂时失败: %s',exc)
                if '409' in str(exc):self.lost.set();return

    def __enter__(self):self.thread.start();return self
    def __exit__(self,*_):self.stop.set();self.thread.join(timeout=20)


def process_task(client,session,task,worker,scratch,shared_root=None,heartbeat=None):
    wid=task['work_id'];lease=task['lease']
    with (nullcontext(heartbeat) if heartbeat else Heartbeat(client,wid,worker,lease)) as heartbeat:
        if heartbeat.lost.is_set():raise RuntimeError('任务租约已丢失，保留本地文件等待重新领取')
        if task['has_candidates']:
            result=client.request('POST',f'/reuse/{wid}/{worker}/{lease}')
            if result.get('asset'):
                LOG.info('复用旧库成功 %s %s',wid,result['asset']['relative_path'])
                return
        folder=scratch/('work-'+wid);folder.mkdir(exist_ok=True)
        url=task['url']
        if task.get('expected_seconds')==0:
            url=url.replace('/video/','/note/')
        LOG.info('开始下载 %s attempt=%s',wid,task['attempts'])
        engine=session.get()
        ok,path,message=engine.download_url(url,folder)
        if not ok:raise RuntimeError(message)
        if task.get('lane') != 'local' and not isinstance(session, PreparedDownload):
            client.request('POST',f'/resolved/{wid}/{worker}/{lease}')
        kind='note' if path.name.startswith('图文-') else 'video'
        try:
            probe_media(path,kind,task.get('expected_seconds'))
        except Exception:
            shutil.rmtree(folder)
            raise
        digest=file_hash(path)
        (folder/'ready.json').write_text(json.dumps({'sha256':digest,'filename':path.name}))
        if heartbeat.lost.is_set():raise RuntimeError('任务租约已丢失，保留本地文件等待重新领取')
        client.request('POST',f'/local-ready/{wid}/{worker}/{lease}')
        # Keep a completed local file while transient transport failures retry;
        # never re-download it simply because the NAS was temporarily offline.
        for attempt in range(5):
            try:
                if shared_root:
                    # An unmounted SMB path must never silently become a local
                    # directory. Match the library identity before any write.
                    marker=shared_root/'索引/library-id'
                    if not marker.is_file() or marker.read_text().strip()!='douyin-library-20261107':
                        raise RuntimeError('SMB素材库未挂载或身份标记不符')
                    partial=shared_root/'临时'/f'shared-{worker}-{lease}.part'
                    ready=partial.with_suffix('.ready')
                    if not ready.exists():
                        if partial.exists():client.request('POST',f'/discard/{wid}/{worker}/{lease}')
                        with path.open('rb') as source,partial.open('xb') as target:
                            shutil.copyfileobj(source,target,length=4*1024*1024)
                            target.flush();os.fsync(target.fileno())
                        partial.replace(ready)
                    asset=client.request('POST',f'/commit/{wid}/{worker}/{lease}',json={'sha256':digest,'size_bytes':path.stat().st_size,'kind':kind})
                else:
                    with path.open('rb') as stream:
                        asset=client.request('PUT',f'/upload/{wid}/{worker}/{lease}',content=stream,
                                             headers={'Content-Length':str(path.stat().st_size),'X-Content-SHA256':digest,'X-Media-Kind':kind})
                LOG.info('入库成功 %s bytes=%s sha256=%s',wid,asset['size_bytes'],digest)
                shutil.rmtree(folder)
                return
            except (httpx.TransportError,RuntimeError) as exc:
                try:
                    status=client.request('GET',f'/works/{wid}')
                    if status.get('status')=='completed' and status.get('sha256')==digest:
                        shutil.rmtree(folder);LOG.info('确认已入库 %s',wid);return
                except Exception:
                    pass
                if attempt==4 or '409' in str(exc) or '422' in str(exc):raise
                LOG.warning('上传重试 %s: %s',wid,exc)
                time.sleep(min(60,5*2**attempt))


def report_failure(client, session, task, worker, scratch, exc, *, phase):
    error = str(exc)
    LOG.error('任务失败 %s phase=%s: %s', task['work_id'], phase, error)
    folder = scratch / ('work-' + task['work_id'])
    keep_local = (folder/'ready.json').exists() and existing_media(folder,task['work_id']) is not None
    reset = is_challenge(error) or is_browser_error(error)
    try:
        result = client.request('POST', f"/fail/{task['work_id']}/{worker}/{task['lease']}",
                                json={'error': error, 'phase': phase, 'has_local_file':keep_local})
        reset = reset or result.get('reset_browser', False)
    except Exception as report:
        LOG.warning('暂未登记失败: %s', report)
        reset = reset or is_missing_metadata(error)
    if folder.exists() and not keep_local:
        shutil.rmtree(folder)
    # Playwright is only touched by its owner (the main thread). Ordinary
    # permission/empty-detail/transfer failures do not invalidate the context.
    if phase == 'resolve' and reset:
        session.reset()


def run_pipeline(client, session, args, stop):
    """One browser owner + one transfer thread, with at most two live leases.

    Queue entries hold their heartbeat from claim through publication, including
    time spent waiting behind a transfer. Shutdown drains the queue before exit.
    """
    pipelined = args.engine == 'metadata'
    capacity = 2 if pipelined else 1
    count = 0
    last_cleanup = 0.
    last_wait_log = 0.
    next_resolve = 0.
    retained = 0
    pending = {}
    LOG.info('启动调度：每槽最多 %s 条在途任务；新任务/重试=9:1；解析与传输%s',
             capacity, '重叠运行' if pipelined else '串行运行')
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='media-transfer') as pool:
        try:
            while True:
                for future, (task, heartbeat) in list(pending.items()):
                    if not future.done():
                        continue
                    try:
                        future.result()
                    except Exception as exc:
                        report_failure(client, session, task, args.worker, args.scratch, exc, phase='transfer')
                    finally:
                        heartbeat.__exit__(None, None, None)
                        del pending[future]
                        last_cleanup = 0 if retained > 64*1024**3 else last_cleanup
                draining = stop.is_set() or (args.limit and count >= args.limit) or (args.scratch/'drain').exists()
                if draining:
                    if not pending:
                        LOG.info('在途任务已处理完毕，工作进程退出')
                        break
                    time.sleep(.2)
                    continue
                if time.monotonic()-session.last_reset >= session.reset_seconds:
                    session.reset()
                active = {task['work_id'] for task, _ in pending.values()}
                if time.monotonic()-last_cleanup >= 1800:
                    retained = maintain_scratch(client, args.scratch, active)
                    last_cleanup = time.monotonic()
                    LOG.info('临时区检查：保留待入库文件 %.2f GiB', retained/1024**3)
                if len(pending) >= capacity:
                    stop.wait(.2)
                    continue
                # Under local disk pressure, still allow existing files to drain.
                local_only = retained > 64*1024**3 or shutil.disk_usage(args.scratch).free < 20*1024**3
                ready = [p.parent.name.removeprefix('work-') for p in args.scratch.glob('work-*/ready.json')
                         if p.parent.name.removeprefix('work-') not in active][:128]
                pacing = time.monotonic() < next_resolve
                try:
                    reply = client.request('POST', '/claim/'+args.worker,
                                           json={'capacity': capacity, 'local_ready': ready,
                                                 'local_only': local_only or pacing})
                except Exception as exc:
                    LOG.warning('任务服务暂不可达: %s', exc)
                    stop.wait(15)
                    continue
                task = reply.get('task')
                if not task:
                    if time.monotonic()-last_wait_log >= 60:
                        LOG.info('等待任务：%s；冷却剩余 %ss；传输在途 %s', reply.get('reason'),
                                 reply.get('remaining_seconds',0),len(pending))
                        last_wait_log = time.monotonic()
                    wait = min(60,max(.2,reply.get('wait',30)))
                    if pacing:
                        wait = min(wait,max(.2,next_resolve-time.monotonic()))
                    # Reap completed transfers and notice graceful shutdown quickly.
                    stop.wait(min(wait, 1) if pending else min(wait, 5))
                    continue
                count += 1
                heartbeat = Heartbeat(client,task['work_id'],args.worker,task['lease'])
                heartbeat.__enter__()
                try:
                    if not pipelined:
                        process_task(client, session, task, args.worker, args.scratch, args.shared_root, heartbeat)
                        heartbeat.__exit__(None,None,None)
                        next_resolve = time.monotonic()+3
                        continue
                    plan = None
                    folder = args.scratch/('work-'+task['work_id'])
                    if not task['has_candidates'] and not existing_media(folder,task['work_id']):
                        if task.get('lane') == 'local':
                            raise RuntimeError('本地任务缺少完整媒体文件，暂缓下载')
                        LOG.info('开始解析 %s attempt=%s；传输在途 %s', task['work_id'],task['attempts'],len(pending))
                        plan = session.get().resolve(task['url'])
                        client.request('POST',f"/resolved/{task['work_id']}/{args.worker}/{task['lease']}")
                        LOG.info('解析完成 %s，交给文件传输队列', task['work_id'])
                    prepared = PreparedDownload(task,plan,args.scratch)
                    future = pool.submit(process_task,client,prepared,task,args.worker,args.scratch,args.shared_root,heartbeat)
                    pending[future] = (task, heartbeat)
                except Exception as exc:
                    report_failure(client,session,task,args.worker,args.scratch,exc,phase='resolve')
                    heartbeat.__exit__(None,None,None)
                finally:
                    if task.get('lane') != 'local':
                        next_resolve = time.monotonic()+3
        finally:
            # Also drain on unexpected main-loop errors; do not abandon a valid
            # upload or stop renewing a lease while its future is still running.
            for future, (task, heartbeat) in pending.items():
                try:
                    future.result()
                except Exception as exc:
                    report_failure(client,session,task,args.worker,args.scratch,exc,phase='transfer')
                finally:
                    heartbeat.__exit__(None,None,None)


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',required=True)
    parser.add_argument('--token-file',type=Path,required=True)
    parser.add_argument('--ca-file',type=Path)
    parser.add_argument('--worker',choices=['mac-1','mac-2','nas-1','nas-2'],required=True)
    parser.add_argument('--scratch',type=Path,required=True)
    parser.add_argument('--shared-root',type=Path,help='Optional mounted SMB library for media transfer')
    parser.add_argument('--seed-state',type=Path)
    parser.add_argument('--reset-seconds',type=int,default=1800)
    parser.add_argument('--engine',choices=['browser','metadata'],default='browser')
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--progress-interval',type=int,default=30,
                        help='mac-1在终端汇总两端进度的间隔秒数，0禁用')
    args=parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    args.scratch.mkdir(parents=True,exist_ok=True)
    with portalocker.Lock(str(args.scratch/'worker.lock'),timeout=0):
        client=Client(args.url,args.token_file.read_text().strip(),args.ca_file)
        factory=None
        if args.engine=='metadata':
            from .library_resolver import IsolatedMetadataResolver
            factory=IsolatedMetadataResolver
        session=BrowserSession(args.scratch/'browser-state.json',args.seed_state,reset_seconds=args.reset_seconds,factory=factory)
        stop = threading.Event()
        reporter = None
        if args.worker == 'mac-1' and args.progress_interval > 0:
            from .library_progress import ProgressReporter
            reporter = ProgressReporter(
                lambda: Client(args.url,args.token_file.read_text().strip(),args.ca_file),
                args.progress_interval)
            reporter.start()
        previous_handlers = {}
        if threading.current_thread() is threading.main_thread():
            def request_stop(signum, frame):
                LOG.info('收到停止信号，停止领取并等待已解析文件入库')
                stop.set()
            for signum in (signal.SIGTERM,signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum,request_stop)
        try:
            run_pipeline(client,session,args,stop)
        finally:
            if reporter:
                reporter.close()
            session.close();client.close()
            for signum, handler in previous_handlers.items():
                signal.signal(signum,handler)


if __name__=='__main__':main()
