"""Single NAS catalogue writer, accessed locally or through an SSH tunnel."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
from contextlib import asynccontextmanager,suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .library_store import LeaseLost, LibraryStore
from .library_store import file_hash


def clean_staging(store):
    with store.connect() as db:
        active={r[0] for r in db.execute("SELECT lease FROM works WHERE status='running' AND lease_until>strftime('%s','now')")}
    for path in (store.root/'临时').iterdir():
        match=re.fullmatch(r'(?:upload-|shared-(?:mac|nas)-[12]-)([a-f0-9]{32})\.(?:part|ready)',path.name)
        if match and match[1] not in active:path.unlink(missing_ok=True)


def create_app(store: LibraryStore, token: str, *, reserve_bytes=100 * 1024**3):
    @asynccontextmanager
    async def lifespan(app):
        async def cleanup_loop():
            while True:
                await asyncio.to_thread(clean_staging,store)
                await asyncio.sleep(1800)
        task=asyncio.create_task(cleanup_loop())
        try:yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):await task
    app = FastAPI(title='Shared Douyin media library',lifespan=lifespan)

    @app.middleware('http')
    async def authenticate(request, call_next):
        if not hmac.compare_digest(request.headers.get('authorization',''), 'Bearer '+token):
            return JSONResponse({'detail':'unauthorized'},status_code=401)
        return await call_next(request)

    @app.exception_handler(LeaseLost)
    async def lease_error(request, exc):
        return JSONResponse({'detail':str(exc)},status_code=409)

    @app.exception_handler(ValueError)
    async def validation_error(request, exc):
        return JSONResponse({'detail':str(exc)},status_code=422)

    @app.get('/status')
    def status():
        return store.status()

    @app.get('/works/{work_id}')
    def locate(work_id: str):
        result=store.locate(work_id)
        if result is None:raise HTTPException(404,'unknown work')
        return result

    @app.post('/claim/{worker}')
    async def claim(worker: str, request: Request):
        if shutil.disk_usage(store.root).free < reserve_bytes:
            return {'task':None,'wait':60,'reason':'low_disk_space'}
        body = await request.json() if await request.body() else {}
        return await asyncio.to_thread(store.claim,worker,
            capacity=int(body.get('capacity',1)), local_ready=body.get('local_ready',[]),
            local_only=bool(body.get('local_only',False)))

    @app.post('/resolved/{work_id}/{worker}/{lease}')
    def resolved(work_id: str, worker: str, lease: str):
        return store.resolved(work_id,worker,lease)

    @app.post('/local-ready/{work_id}/{worker}/{lease}')
    def local_ready(work_id: str, worker: str, lease: str):
        return store.local_ready(work_id,worker,lease)

    @app.post('/heartbeat/{work_id}/{worker}/{lease}')
    def heartbeat(work_id: str, worker: str, lease: str):
        store.heartbeat(work_id,worker,lease)
        return {'ok':True}

    @app.post('/fail/{work_id}/{worker}/{lease}')
    async def fail(work_id: str, worker: str, lease: str, request: Request):
        body=await request.json()
        return await asyncio.to_thread(store.fail,work_id,worker,lease,
            str(body.get('error','unknown')),phase=str(body.get('phase','resolve')),
            has_local_file=body.get('has_local_file'))

    @app.post('/reuse/{work_id}/{worker}/{lease}')
    def reuse(work_id: str, worker: str, lease: str):
        return {'asset':store.reuse(work_id,worker,lease)}

    @app.post('/discard/{work_id}/{worker}/{lease}')
    def discard(work_id: str, worker: str, lease: str):
        with store.connect() as db:store._check(db,work_id,worker,lease)
        for suffix in ('part','ready'):
            (store.root/'临时'/f'shared-{worker}-{lease}.{suffix}').unlink(missing_ok=True)
        return {'ok':True}

    @app.post('/commit/{work_id}/{worker}/{lease}')
    async def commit(work_id: str, worker: str, lease: str, request: Request):
        """Validate a completed SMB transfer; only NAS writes the catalogue."""
        with store.connect() as db:store._check(db,work_id,worker,lease)
        body=await request.json()
        staged=store.root/'临时'/f'shared-{worker}-{lease}.ready'
        expected_size=int(body['size_bytes'])
        if not 1024 < expected_size <= 64*1024**3 or staged.stat().st_size!=expected_size:
            raise ValueError('SMB传输文件大小不符')
        digest=await asyncio.to_thread(file_hash,staged)
        if digest!=body['sha256']:raise ValueError('SMB传输文件校验不符')
        return await asyncio.to_thread(store.publish,work_id,worker,lease,staged,body['kind'],digest)

    @app.put('/upload/{work_id}/{worker}/{lease}')
    async def upload(work_id: str, worker: str, lease: str, request: Request):
        with store.connect() as db:
            store._check(db,work_id,worker,lease)
        expected_hash=request.headers.get('x-content-sha256','')
        kind=request.headers.get('x-media-kind','')
        expected_size=int(request.headers.get('content-length','0'))
        if expected_size <= 1024 or expected_size > 64*1024**3:
            raise HTTPException(413,'invalid file size')
        if shutil.disk_usage(store.root).free < reserve_bytes+expected_size:
            raise HTTPException(507,'insufficient free space')
        # The lease comes from a database equality check, never a caller's path.
        staging=store.root/'临时'/('upload-'+lease+'.part')
        digest=hashlib.sha256();total=0;created=False
        try:
            with staging.open('xb') as out:
                created=True
                async for chunk in request.stream():
                    total+=len(chunk)
                    if total > expected_size:raise HTTPException(413,'body exceeds advertised size')
                    out.write(chunk);digest.update(chunk)
                out.flush();os.fsync(out.fileno())
            if total != expected_size or digest.hexdigest() != expected_hash:
                raise HTTPException(422,'transfer checksum mismatch')
            return await asyncio.to_thread(store.publish,work_id,worker,lease,staging,kind,expected_hash)
        finally:
            if created:staging.unlink(missing_ok=True)

    return app


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--roots-file',type=Path)
    parser.add_argument('--token-file',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8013)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--ssl-cert',type=Path)
    parser.add_argument('--ssl-key',type=Path)
    args=parser.parse_args(argv)
    roots=json.loads(args.roots_file.read_text()) if args.roots_file else {}
    store=LibraryStore(args.root,roots=roots)
    token=args.token_file.read_text().strip()
    if len(token)<32:raise ValueError('API token too short')
    clean_staging(store)
    import uvicorn
    uvicorn.run(create_app(store,token),host=args.host,port=args.port,access_log=False,
                ssl_certfile=str(args.ssl_cert) if args.ssl_cert else None,
                ssl_keyfile=str(args.ssl_key) if args.ssl_key else None)


if __name__=='__main__':
    main()
