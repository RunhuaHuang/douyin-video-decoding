"""Offline maintenance: stop the catalogue service before running mutations."""
import argparse
import json
import os
import time
from pathlib import Path

from .library_store import LibraryStore,file_hash


def flatten(store):
    actions=[]
    with store.connect() as db:
        rows=db.execute("SELECT * FROM assets WHERE storage_root='new'").fetchall()
    for row in rows:
        prefix='图文' if row['kind']=='note' else '视频'
        new=Path('媒体库')/f"{prefix}-{row['work_id']}.mp4"
        if new.as_posix()==row['relative_path']:continue
        old=store.root/row['relative_path'];target=store.root/new
        if not old.exists():raise ValueError('Missing indexed source: '+str(old))
        if file_hash(old)!=row['sha256']:raise ValueError('Checksum mismatch: '+str(old))
        if target.exists():raise ValueError('Destination already exists: '+str(target))
        actions.append({'asset_id':row['asset_id'],'old':str(old),'new':str(target),'relative_path':new.as_posix()})
    journal=store.root/'任务'/f'flat-layout-{time.time_ns()}.json'
    journal.write_text(json.dumps(actions,ensure_ascii=False,indent=2))
    moved=[]
    try:
        with store.transaction() as db:
            for a in actions:
                os.rename(a['old'],a['new']);moved.append(a)
                db.execute('UPDATE assets SET relative_path=? WHERE asset_id=?',(a['relative_path'],a['asset_id']))
    except BaseException:
        for a in reversed(moved):os.rename(a['new'],a['old'])
        raise
    for path in (store.root/'媒体库').iterdir():
        if path.is_dir() and len(path.name)==2 and not any(path.iterdir()):path.rmdir()
    return {'renamed':len(actions),'journal':str(journal)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['flatten','release-nas'])
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args();store=LibraryStore(args.root)
    if args.command=='flatten':print(json.dumps(flatten(store),ensure_ascii=False))
    else:
        with store.transaction() as db:
            db.execute("UPDATE works SET status='retry',retry_at=?,lease=NULL WHERE assigned_worker='nas' AND status='running'",(time.time(),))


if __name__=='__main__':main()
