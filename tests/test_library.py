import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.library_api import create_app
from app.library_store import LeaseLost, LibraryStore
from app.library_worker import BrowserSession


@pytest.fixture
def store(tmp_path):
    return LibraryStore(tmp_path/'library',lease_seconds=300)


def seed(store, n=4):
    with store.transaction() as db:
        for i in range(n):
            db.execute('INSERT INTO works(work_id,url,expected_seconds,assigned_worker,priority,updated_at) VALUES(?,?,?,?,?,?)',
                       (str(7600000000000000000+i),'https://www.douyin.com/video/'+str(7600000000000000000+i),30,'mac' if i%2==0 else 'nas',i,time.time()))


def test_four_workers_claim_distinct_tasks(store):
    seed(store)
    with ThreadPoolExecutor(4) as pool:
        results=list(pool.map(store.claim,['mac-1','nas-1','mac-2','nas-2']))
    assert len({r['task']['work_id'] for r in results})==4
    assert store.claim('mac-1')['reason']=='slot_busy'


def test_cooldown_is_per_host(store):
    seed(store)
    task=store.claim('mac-1')['task']
    store.fail(task['work_id'],'mac-1',task['lease'],'验证码 captcha')
    assert store.claim('mac-2')['reason']=='host_cooldown'
    assert store.claim('nas-1')['task']


def test_missing_metadata_circuit_breaker_is_per_host(store):
    seed(store,10)
    for i in range(3):
        task=store.claim('mac-1')['task']
        store.fail(task['work_id'],'mac-1',task['lease'],'未捕获到视频元数据')
        with store.connect() as db:
            row=db.execute('SELECT status,attempts FROM works WHERE work_id=?',(task['work_id'],)).fetchone()
            assert row['status']=='retry' and row['attempts']==0
    assert store.claim('mac-2')['reason']=='host_cooldown'
    assert store.claim('nas-1')['task']


def test_successful_resolution_resets_metadata_failure_streak(store):
    seed(store,8)
    task=store.claim('mac-1')['task']
    store.fail(task['work_id'],'mac-1',task['lease'],'未捕获到视频元数据')
    task=store.claim('mac-2')['task']
    store.resolved(task['work_id'],'mac-2',task['lease'])
    with store.connect() as db:
        assert db.execute("SELECT value FROM meta WHERE key='metadata_failures:mac'").fetchone() is None


def test_stale_lease_cannot_publish(store):
    seed(store)
    task=store.claim('mac-1')['task']
    with store.transaction() as db:db.execute('UPDATE works SET lease_until=0 WHERE work_id=?',(task['work_id'],))
    new=store.claim('mac-2')['task']
    # Fresh work gets precedence; the expired lease is recovered afterwards.
    store.fail(new['work_id'],'mac-2',new['lease'],'ordinary failure')
    new=store.claim('mac-2')['task']
    assert new['work_id']==task['work_id']
    assert new['lease']!=task['lease']
    with pytest.raises(LeaseLost):store.heartbeat(task['work_id'],'mac-1',task['lease'])


def test_upload_checks_hash_and_preserves_sources(store,monkeypatch):
    seed(store)
    monkeypatch.setattr('app.library_store.probe_media',lambda *a:{'duration':30,'width':720,'height':1280})
    app=TestClient(create_app(store,'secret',reserve_bytes=0))
    headers={'Authorization':'Bearer secret'}
    assert app.get('/status').status_code==401
    task=app.post('/claim/mac-1',headers=headers).json()['task']
    payload=b'test video'*300
    route=f"/upload/{task['work_id']}/mac-1/{task['lease']}"
    upload_headers={**headers,'X-Media-Kind':'video','X-Content-SHA256':'0'*64}
    assert app.put(route,headers=upload_headers,content=payload).status_code==422
    assert not list((store.root/'临时').iterdir())
    digest=hashlib.sha256(payload).hexdigest()
    upload_headers['X-Content-SHA256']=digest
    result=app.put(route,headers=upload_headers,content=payload)
    assert result.status_code==200,result.text
    location=app.get('/works/'+task['work_id'],headers=headers).json()
    assert location['sha256']==digest and location['exists']
    assert location['status']=='completed'
    assert not list((store.root/'临时').iterdir())


def test_identical_bytes_share_one_file(store,monkeypatch):
    seed(store)
    monkeypatch.setattr('app.library_store.probe_media',lambda *a:{'duration':30})
    digest=hashlib.sha256(b'a'*2048).hexdigest()
    for worker in ['mac-1','nas-1']:
        task=store.claim(worker)['task']
        path=store.root/'临时'/worker;path.write_bytes(b'a'*2048)
        store.publish(task['work_id'],worker,task['lease'],path,'video',digest)
    assert len(list((store.root/'媒体库').rglob('*.mp4')))==1
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM works WHERE status='completed'").fetchone()[0]==2
    assert store.status()['completion_counts'] == {
        host: {'new_download': 1, 'old_reuse': 0, 'unclassified': 0}
        for host in ('mac', 'nas')}


def test_progress_separates_reuse_from_download_speed(store):
    from app.library_progress import format_progress
    seed(store, 8)
    before = store.status()
    store.roots['old'] = str(store.root/'old')
    for worker, root in [('mac-1', 'new'), ('mac-2', 'old'), ('nas-1', 'old')]:
        task = store.claim(worker)['task']
        path = store.resolve(root, task['work_id']+'.mp4')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'validated media')
        store.register(task['work_id'], worker, task['lease'], path, 'video', {}, 'hash', root)
    status = store.status()
    assert status['completion_counts']['mac'] == {'new_download': 1, 'old_reuse': 1, 'unclassified': 0}
    assert status['completion_counts']['nas'] == {'new_download': 0, 'old_reuse': 1, 'unclassified': 0}
    output = format_progress(status, before, 30)
    assert '新下载 +1 | 旧库复用 +1 | 下载速度 2.0 条/分钟' in output
    assert '新下载 +0 | 旧库复用 +1 | 下载速度 0.0 条/分钟' in output
    assert '等待下一轮采样' in format_progress(status)
    assert '统计基线已变更' in format_progress(before, status, 30)
    assert '等待服务提供分类统计' in format_progress({'counts': []})


def test_moved_old_file_is_not_redownloaded_silently(store):
    seed(store)
    task=store.claim('mac-1')['task']
    store.roots['old']=str(store.root/'old')
    with store.transaction() as db:db.execute('INSERT INTO candidates VALUES(?,?,?,?,?)',(task['work_id'],'old','moved.mp4',4096,'video'))
    with pytest.raises(ValueError,match='暂缓下载'):store.reuse(task['work_id'],'mac-1',task['lease'])


def test_user_selected_longer_media_reuses_without_changing_source_duration(store, monkeypatch):
    seed(store)
    task = store.claim('mac-1')['task']
    store.roots['old'] = str(store.root/'old')
    old = store.root/'old'; old.mkdir()
    path = old/'selected.mp4'; path.write_bytes(b'long media' * 200)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with store.transaction() as db:
        db.execute('UPDATE works SET expected_seconds=9 WHERE work_id=?', (task['work_id'],))
        db.execute('INSERT INTO candidates VALUES(?,?,?,?,?)', (task['work_id'],'old',path.name,path.stat().st_size,'video'))
        db.execute('INSERT INTO user_media_choices VALUES(?,?,?,?,?)', (task['work_id'],'old',path.name,'video',digest))
    observed = []
    def probe(path, kind, expected):
        observed.append(expected)
        return {'duration': 120}
    monkeypatch.setattr('app.library_store.probe_media', probe)
    result = store.reuse(task['work_id'], 'mac-1', task['lease'])
    assert result['sha256'] == digest and observed == [None]
    with store.connect() as db:
        assert db.execute('SELECT expected_seconds FROM works WHERE work_id=?', (task['work_id'],)).fetchone()[0] == 9


def test_user_selected_changed_media_fails_closed_even_with_alternate_candidate(store, monkeypatch):
    seed(store)
    task = store.claim('mac-1')['task']
    store.roots['old'] = str(store.root/'old')
    old = store.root/'old'; old.mkdir()
    path = old/'selected.mp4'; path.write_bytes(b'changed media' * 200)
    with store.transaction() as db:
        db.execute('INSERT INTO candidates VALUES(?,?,?,?,?)', (task['work_id'],'old',path.name,path.stat().st_size,'video'))
        db.execute('INSERT INTO user_media_choices VALUES(?,?,?,?,?)', (task['work_id'],'old',path.name,'video','0'*64))
    monkeypatch.setattr('app.library_store.probe_media', lambda *args: {'duration': 120})
    with pytest.raises(ValueError, match='暂缓下载'):
        store.reuse(task['work_id'], 'mac-1', task['lease'])


def test_user_choice_without_candidate_claims_local_and_never_redownloads(store):
    seed(store)
    with store.transaction() as db:
        wid = db.execute("SELECT work_id FROM works WHERE assigned_worker='mac' ORDER BY priority LIMIT 1").fetchone()[0]
        db.execute('INSERT INTO user_media_choices VALUES(?,?,?,?,?)', (wid, 'new', 'missing.mp4', 'video', '0'*64))
    task = store.claim('mac-1', local_only=True)['task']
    assert task['work_id'] == wid and task['has_candidates'] and task['lane'] == 'local'
    with pytest.raises(ValueError, match='暂缓下载'):
        store.reuse(wid, 'mac-1', task['lease'])


def test_user_choice_with_unmounted_root_fails_closed(store):
    seed(store)
    with store.transaction() as db:
        wid = db.execute("SELECT work_id FROM works WHERE assigned_worker='mac' ORDER BY priority LIMIT 1").fetchone()[0]
        db.execute('INSERT INTO user_media_choices VALUES(?,?,?,?,?)', (wid, 'unmounted', 'file.mp4', 'video', '0'*64))
    task = store.claim('mac-1', local_only=True)['task']
    with pytest.raises(ValueError, match='暂缓下载'):
        store.reuse(wid, 'mac-1', task['lease'])


def test_user_choice_can_override_source_type_hint_without_changing_it(store, monkeypatch):
    seed(store)
    task = store.claim('mac-1')['task']
    path = store.root/'selected.mp4'; path.write_bytes(b'video' * 400)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with store.transaction() as db:
        db.execute('UPDATE works SET expected_seconds=0 WHERE work_id=?', (task['work_id'],))
        db.execute('INSERT INTO user_media_choices VALUES(?,?,?,?,?)', (task['work_id'],'new',path.name,'video',digest))
    monkeypatch.setattr('app.library_store.probe_media', lambda p,k,e: {'duration':120} if k=='video' and e is None else pytest.fail('wrong validation'))
    assert store.reuse(task['work_id'],'mac-1',task['lease'])['sha256']==digest
    with store.connect() as db:
        assert db.execute('SELECT expected_seconds FROM works WHERE work_id=?',(task['work_id'],)).fetchone()[0]==0


def test_session_reset_discards_working_cookies_only(tmp_path):
    seedfile=tmp_path/'account.json'
    seedfile.write_text(json.dumps({'cookies':[{'name':'sessionid','value':'private'}],'origins':[]}))
    instances=[]
    class Engine:
        def __init__(self,**kwargs):self.closed=False;instances.append(self)
        def close(self):self.closed=True
    session=BrowserSession(tmp_path/'work/state.json',seedfile,reset_seconds=1800,factory=Engine)
    session.get();session.last_reset-=1801;session.get()
    assert instances[0].closed and len(instances)==2
    assert json.loads(session.path.read_text())=={'cookies':[],'origins':[]}
    assert json.loads(seedfile.read_text())['cookies'][0]['value']=='private'


def test_path_cannot_escape_root(store):
    with pytest.raises(ValueError):store.resolve('new','../../secret')


def test_import_preserves_duplicate_source_rows_and_same_named_tables(store,tmp_path):
    import openpyxl
    from app.library_import import import_sources
    sources=[]
    for name in ['种草类目','视频分类']:
        directory=tmp_path/name;directory.mkdir();sources.append(directory)
        wb=openpyxl.Workbook();ws=wb.active
        ws.append(['视频链接','视频时长','点赞数'])
        ws.append(['https://www.douyin.com/video/7600000000000000001','30s',100 if name=='种草类目' else 200])
        wb.save(directory/'医疗健康.xlsx')
    result=import_sources(store,sources)
    assert result['unique_works']==1 and result['source_rows']==2
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM sources').fetchone()[0]==2
        values=[json.loads(r[0])['点赞数'] for r in db.execute('SELECT attributes_json FROM source_rows')]
        assert sorted(values)==[100,200]


def test_smb_commit_and_cleanup_protect_live_transfer(store,monkeypatch):
    from app.library_api import clean_staging
    seed(store)
    task=store.claim('mac-1')['task'];lease=task['lease']
    path=store.root/'临时'/f'shared-mac-1-{lease}.ready'
    payload=b'valid media'*1000;path.write_bytes(payload)
    stale=store.root/'临时'/('shared-mac-2-'+'a'*32+'.part');stale.write_bytes(b'old')
    clean_staging(store)
    assert path.exists() and not stale.exists()
    monkeypatch.setattr('app.library_store.probe_media',lambda *a:{'duration':30})
    client=TestClient(create_app(store,'token',reserve_bytes=0))
    response=client.post(f"/commit/{task['work_id']}/mac-1/{lease}",headers={'Authorization':'Bearer token'},json={'size_bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),'kind':'video'})
    assert response.status_code==200,response.text
    assert not path.exists()
    assert store.locate(task['work_id'])['status']=='completed'


def test_flatten_preserves_asset_identity_and_lookup(store,monkeypatch):
    from app.library_admin import flatten
    seed(store)
    task=store.claim('mac-1')['task']
    payload=b'media'*500;digest=hashlib.sha256(payload).hexdigest()
    old=store.root/'媒体库'/digest[:2]/(digest+'.mp4');old.parent.mkdir();old.write_bytes(payload)
    asset=store.register(task['work_id'],'mac-1',task['lease'],old,'video',{'duration':30},digest)
    result=flatten(store)
    location=store.locate(task['work_id'])
    assert result['renamed']==1 and location['asset_id']==asset['asset_id']
    assert location['relative_path']==f"媒体库/视频-{task['work_id']}.mp4"
    assert not old.exists() and location['exists']
    assert not [p for p in (store.root/'媒体库').iterdir() if p.is_dir()]


def test_metadata_adapter_rejects_wrong_work(tmp_path):
    from app.library_downloader import MetadataDownloader
    engine=object.__new__(MetadataDownloader)
    class Worker:
        def resolve_video_info(self,wid):return {'aweme_id':'wrong'},None
    engine.worker=Worker()
    ok,path,message=engine.download_url('https://www.douyin.com/video/7600000000000000001',tmp_path)
    assert not ok and 'ID不符' in message


def finish_for_test(store, task, worker='mac-1'):
    with store.transaction() as db:
        db.execute("UPDATE works SET status='completed' WHERE work_id=?", (task['work_id'],))


def test_new_tasks_get_nine_of_ten_claims_with_retries_waiting(store):
    seed(store, 50)
    with store.transaction() as db:
        db.execute("UPDATE works SET status='retry',retry_at=0 WHERE assigned_worker='mac' AND priority<10")
    statuses=[]
    for _ in range(20):
        task=store.claim('mac-1')['task']
        statuses.append(task['status'])
        finish_for_test(store,task)
    assert statuses == (['pending']*9+['retry'])*2
    # Once fresh work is exhausted the remaining retry queue is still serviced.
    for _ in range(2):
        task=store.claim('mac-1')['task']
        finish_for_test(store,task)
    assert store.claim('mac-1')['task']['status']=='retry'


def test_permissions_are_deferred_six_hours_without_cooling_host(store):
    seed(store)
    task=store.claim('mac-1')['task']
    now=time.time()
    result=store.fail(task['work_id'],'mac-1',task['lease'],'访问受限(作品权限)')
    with store.connect() as db:
        row=db.execute('SELECT retry_at,status FROM works WHERE work_id=?',(task['work_id'],)).fetchone()
        assert row['retry_at'] >= now+6*3600 and row['status']=='retry'
    assert result['reset_browser'] is False
    assert store.claim('mac-2')['task']


def test_cooldown_allows_candidates_and_ready_files_but_not_remote_work(store):
    seed(store,10)
    task=store.claim('mac-1')['task']
    store.fail(task['work_id'],'mac-1',task['lease'],'验证码')
    with store.transaction() as db:
        db.execute('UPDATE works SET retry_at=0 WHERE work_id=?',(task['work_id'],))
        db.execute('INSERT INTO candidates VALUES(?,?,?,?,?)',('7600000000000000002','old','v.mp4',12345,'video'))
    ready=store.claim('mac-1',local_ready=[task['work_id']])['task']
    assert ready['work_id']==task['work_id'] and ready['lane']=='local'
    finish_for_test(store,ready)
    reused=store.claim('mac-2')['task']
    assert reused['has_candidates'] and reused['lane']=='local'
    finish_for_test(store,reused)
    assert store.claim('mac-1')['reason']=='host_cooldown'
    assert store.claim('nas-1')['task']


def test_local_success_does_not_clear_parser_failure_history(store):
    seed(store,10)
    for _ in range(3):
        task=store.claim('mac-1')['task']
        store.fail(task['work_id'],'mac-1',task['lease'],'未捕获到视频元数据')
    with store.transaction() as db:
        db.execute('INSERT INTO candidates VALUES(?,?,?,?,?)',('7600000000000000006','old','v.mp4',12345,'video'))
    task=store.claim('mac-1')['task']
    media=store.root/'临时'/'v.mp4';media.write_bytes(b'media')
    store.register(task['work_id'],'mac-1',task['lease'],media,'video',{},'hash')
    with store.connect() as db:
        assert db.execute("SELECT value FROM meta WHERE key='metadata_failures:mac'").fetchone()[0]=='3'
    assert store.claim('mac-1')['reason']=='host_cooldown'


def test_two_leases_are_bounded_renewable_and_exclusive(store):
    seed(store,10)
    tasks=[]
    for _ in range(2):
        with store.transaction() as db:db.execute("DELETE FROM meta WHERE key='next_resolve_at:mac'")
        tasks.append(store.claim('mac-1',capacity=2)['task'])
    assert store.claim('mac-1',capacity=2)['reason']=='slot_busy'
    for task in tasks:store.heartbeat(task['work_id'],'mac-1',task['lease'])
    with store.transaction() as db:db.execute("DELETE FROM meta WHERE key='next_resolve_at:mac'")
    other=store.claim('mac-2',capacity=2)['task']
    assert other['work_id'] not in {t['work_id'] for t in tasks}
    with pytest.raises(ValueError):store.claim('mac-1',capacity=3)


def test_ordinary_failure_preserves_browser_but_breaker_resets_it(store,tmp_path):
    from app.library_worker import report_failure
    seed(store,12)
    class Session:
        resets=0
        def reset(self):self.resets+=1
    session=Session()
    class Client:
        def request(self,method,path,json):
            _,_,wid,worker,lease=path.split('/')
            return store.fail(wid,worker,lease,json['error'],phase=json['phase'])
    client=Client()
    for error in ['访问受限(权限)','未捕获到有效视频数据(空详情)']:
        task=store.claim('mac-1')['task']
        report_failure(client,session,task,'mac-1',tmp_path,RuntimeError(error),phase='resolve')
    assert session.resets==0
    for _ in range(3):
        task=store.claim('mac-1')['task']
        report_failure(client,session,task,'mac-1',tmp_path,RuntimeError('未捕获到视频元数据'),phase='resolve')
    assert session.resets==1


def test_cleanup_never_removes_active_download(tmp_path):
    from app.library_worker import maintain_scratch
    active=tmp_path/'work-123';active.mkdir();(active/'partial.mp4').write_bytes(b'part')
    orphan=tmp_path/'work-456';orphan.mkdir();(orphan/'partial.mp4').write_bytes(b'part')
    maintain_scratch(None,tmp_path,active={'123'})
    assert active.exists() and not orphan.exists()


def test_pipeline_resolves_next_item_while_transfer_is_blocked(store,tmp_path,monkeypatch):
    import threading
    from types import SimpleNamespace
    from app.library_worker import run_pipeline
    seed(store,8)
    started=threading.Event();overlapped=threading.Event()
    main_thread=threading.get_ident()
    resolved=[];transferred=[]
    class Session:
        last_reset=time.monotonic()
        reset_seconds=1800
        def get(self):return self
        def reset(self):raise AssertionError('unexpected reset')
        def resolve(self,url):
            assert threading.get_ident()==main_thread
            resolved.append(url)
            if len(resolved)==2:
                assert started.wait(3)
                overlapped.set()
            return {'detail':{'aweme_id':url.rsplit('/',1)[1]}}
    class Client:
        def request(self,method,path,**kwargs):
            if path.startswith('/claim/'):
                # Avoid testing wall-clock pacing here; use real leases/scheduling.
                with store.transaction() as db:db.execute("DELETE FROM meta WHERE key='next_resolve_at:mac'")
                return store.claim(path.split('/')[-1],**kwargs.get('json',{}))
            _,operation,wid,worker,lease=path.split('/')
            if operation=='resolved':return store.resolved(wid,worker,lease)
            if operation=='heartbeat':return store.heartbeat(wid,worker,lease)
            if operation=='fail':return store.fail(wid,worker,lease,kwargs['json']['error'])
            raise AssertionError(path)
    def transfer(client,prepared,task,worker,scratch,shared_root,heartbeat):
        assert threading.get_ident()!=main_thread
        assert not heartbeat.lost.is_set()
        started.set()
        assert overlapped.wait(10), 'next resolution was blocked by transfer'
        transferred.append(task['work_id'])
        finish_for_test(store,task)
    monkeypatch.setattr('app.library_worker.process_task',transfer)
    # Keep the scratch disk reserve check independent of the test machine.
    monkeypatch.setattr('app.library_worker.shutil.disk_usage',lambda _:SimpleNamespace(free=100*1024**3))
    args=SimpleNamespace(engine='metadata',worker='mac-1',scratch=tmp_path,shared_root=None,limit=2)
    run_pipeline(Client(),Session(),args,threading.Event())
    assert len(resolved)==len(transferred)==2
    assert not any(t.name=='media-transfer_0' for t in threading.enumerate())


def test_prepared_transfer_publishes_then_confirms_before_removing_local_file(store,tmp_path,monkeypatch):
    import functools
    import http.server
    import subprocess
    import threading
    from app.library_downloader import PreparedDownload
    from app.library_worker import process_task
    from batch_download_huitun import TableWorker
    seed(store)
    source=tmp_path/'http';source.mkdir()
    media=source/'sample.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x120:rate=10',
                    '-f','lavfi','-i','sine=frequency=440','-t','2','-c:v','libx264',
                    '-c:a','aac',str(media)],check=True)
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(source)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    api=TestClient(create_app(store,'token',reserve_bytes=0))
    class Client:
        def request(self,method,path,**kwargs):
            headers={'Authorization':'Bearer token',**kwargs.pop('headers',{})}
            response=api.request(method,path,headers=headers,**kwargs)
            response.raise_for_status()
            return response.json()
    monkeypatch.setattr(TableWorker,'start_browser',lambda _:pytest.fail('transfer opened a browser'))
    scratch=tmp_path/'scratch';scratch.mkdir()
    marker=store.root/'索引/library-id';marker.write_text('douyin-library-20261107')
    task=store.claim('mac-1')['task']
    with store.transaction() as db:db.execute('UPDATE works SET expected_seconds=2 WHERE work_id=?',(task['work_id'],))
    task['expected_seconds']=2
    plan={'detail':{'aweme_id':task['work_id'],'video':{'play_addr':{'url_list':[f'http://127.0.0.1:{server.server_port}/sample.mp4']}}}}
    try:
        store.resolved(task['work_id'],'mac-1',task['lease'])
        process_task(Client(),PreparedDownload(task,plan,scratch),task,'mac-1',scratch,store.root)
        result=store.locate(task['work_id'])
        assert result['status']=='completed' and result['exists']
        assert result['sha256']==hashlib.sha256(media.read_bytes()).hexdigest()
        assert not list(scratch.glob('work-*'))
    finally:
        server.shutdown();server.server_close();thread.join()


def test_transfer_failure_keeps_ready_file_and_reclaims_during_cooldown(store,tmp_path,monkeypatch):
    from app.library_worker import process_task, report_failure
    from app.library_downloader import PreparedDownload
    seed(store)
    task=store.claim('mac-1')['task']
    folder=tmp_path/('work-'+task['work_id']);folder.mkdir()
    path=folder/f"视频-{task['work_id']}.mp4";path.write_bytes(b'media'*500)
    monkeypatch.setattr('app.library_worker.probe_media',lambda *args:{'duration':30})
    class Client:
        def request(self,method,path,**kwargs):
            if path.startswith('/local-ready/'):
                _,_,wid,worker,lease=path.split('/')
                return store.local_ready(wid,worker,lease)
            if path.startswith('/upload/'):
                raise RuntimeError('API 422: test transport failure')
            if path.startswith('/works/'):
                return {'status':'running'}
            if path.startswith('/fail/'):
                _,_,wid,worker,lease=path.split('/')
                return store.fail(wid,worker,lease,kwargs['json']['error'],phase=kwargs['json']['phase'])
            raise AssertionError(path)
    class Session:
        def reset(self):pytest.fail('transfer failure reset browser')
    client=Client()
    with pytest.raises(RuntimeError,match='422') as error:
        process_task(client,PreparedDownload(task,None,tmp_path),task,'mac-1',tmp_path)
    report_failure(client,Session(),task,'mac-1',tmp_path,error.value,phase='transfer')
    assert path.exists() and (folder/'ready.json').exists()
    with store.transaction() as db:
        db.execute('UPDATE works SET retry_at=0 WHERE work_id=?',(task['work_id'],))
        db.execute('INSERT INTO meta VALUES(?,?)',('cooldown_until:mac',str(time.time()+900)))
    next_task=store.claim('mac-1',local_ready=[task['work_id']])['task']
    assert next_task['work_id']==task['work_id'] and next_task['lane']=='local'
    assert next_task['lease']!=task['lease']
    with pytest.raises(LeaseLost):store.resolved(task['work_id'],'mac-1',task['lease'])


def test_ready_file_is_reserved_for_its_slot_until_uploaded(store):
    seed(store,1)
    task=store.claim('mac-1')['task']
    store.local_ready(task['work_id'],'mac-1',task['lease'])
    store.fail(task['work_id'],'mac-1',task['lease'],'upload unavailable',phase='transfer',has_local_file=True)
    with store.transaction() as db:db.execute('UPDATE works SET retry_at=0')
    assert store.claim('mac-2')['reason']=='no_ready_tasks'
    recovered=store.claim('mac-1',local_ready=[task['work_id']])['task']
    assert recovered['work_id']==task['work_id']
    # If validation finds the retained file corrupt, release the reservation.
    store.fail(recovered['work_id'],'mac-1',recovered['lease'],'corrupt',phase='transfer',has_local_file=False)
    with store.transaction() as db:db.execute('UPDATE works SET retry_at=0')
    assert store.claim('mac-2')['task']['work_id']==task['work_id']
