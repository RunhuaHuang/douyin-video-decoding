"""Import source attributes and assign one download task per work, without browsing."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import time
import warnings
from pathlib import Path

import openpyxl

from .library_store import LibraryStore, file_hash


def import_sources(store, source_dirs):
    works={};source_count=0;row_count=0
    with store.transaction() as db:
        if db.execute('SELECT 1 FROM works LIMIT 1').fetchone():
            raise ValueError('只允许初始化空任务库，避免覆盖正在运行的任务')
        for directory in source_dirs:
            directory=Path(directory).resolve()
            for path in sorted(directory.glob('*.xlsx')):
                if path.name.startswith('~$'):continue
                digest=file_hash(path)
                sid=hashlib.sha256((directory.name+'/'+path.name+'\0'+digest).encode()).hexdigest()
                relative=Path('来源表')/directory.name/path.name
                target=store.root/relative;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,target)
                db.execute('INSERT INTO sources VALUES(?,?,?,?,?)',(sid,directory.name,path.name,digest,str(relative)))
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore',UserWarning)
                    wb=openpyxl.load_workbook(path,read_only=True,data_only=True)
                try:
                    for sheet in wb:
                        sheet.reset_dimensions()
                        rows=sheet.iter_rows(values_only=True)
                        header=next(rows)
                        for number,row in enumerate(rows,2):
                            if not any(v is not None for v in row):continue
                            attrs=dict(zip(header,row))
                            url=str(attrs.get('视频链接') or attrs.get('链接') or '')
                            match=re.search(r'(?:video/|note/|modal_id=)(\d{15,22})',url)
                            if not match:raise ValueError(f'无法提取作品ID: {path}:{sheet.title}:{number}')
                            wid=match[1]
                            parts=re.findall(r'([\d.]+)\s*([hms])',str(attrs.get('视频时长') or ''))
                            seconds=sum(float(n)*{'h':3600,'m':60,'s':1}[unit] for n,unit in parts) if parts else None
                            works.setdefault(wid,{'url':url,'seconds':seconds})
                            db.execute('INSERT INTO source_rows VALUES(?,?,?,?,?)',(sid,sheet.title,number,wid,json.dumps(attrs,ensure_ascii=False,default=str)))
                            row_count+=1
                finally:
                    wb.close()
                source_count+=1
                if source_count%100==0:print(f'已导入 {source_count} 张表 / {row_count} 条来源记录',flush=True)
        # Equal counts, approximately balanced aggregate source duration.
        limits=[(len(works)+1)//2,len(works)//2];groups=[[],[]];totals=[0.,0.]
        for wid,item in sorted(works.items(),key=lambda x:(-(x[1]['seconds'] or 0),x[0])):
            host=min((i for i in range(2) if len(groups[i])<limits[i]),key=lambda i:(totals[i],len(groups[i])))
            groups[host].append(wid);totals[host]+=item['seconds'] or 0
        rng=random.Random(20261107)
        for i,group in enumerate(groups):
            rng.shuffle(group)
            # Start with small real videos, including one >5 minute stream, so
            # both workers validate the complete pipeline before large files.
            short=next((w for w in group if 10 <= (works[w]['seconds'] or 0) <= 45),None)
            long=next((w for w in group if 310 <= (works[w]['seconds'] or 0) <= 360),None)
            first=[w for w in (short,long) if w]
            order=first+[w for w in group if w not in first]
            for priority,wid in enumerate(order):
                db.execute('INSERT INTO works(work_id,url,expected_seconds,assigned_worker,priority,updated_at) VALUES(?,?,?,?,?,?)',(wid,works[wid]['url'],works[wid]['seconds'],('nas','mac')[i],priority,time.time()))
        summary={'source_tables':source_count,'source_rows':row_count,'unique_works':len(works),'nas_count':len(groups[0]),'mac_count':len(groups[1]),'nas_video_hours':totals[0]/3600,'mac_video_hours':totals[1]/3600}
        db.execute('INSERT INTO meta VALUES(?,?)',('import_summary',json.dumps(summary)))
    return summary


def import_candidates(store, inventory_path, root_prefixes):
    count=0
    with store.transaction() as db:
        known={r[0] for r in db.execute('SELECT work_id FROM works')}
        for line in Path(inventory_path).read_text().splitlines():
            size,path=line.split('\t',1)
            p=Path(path)
            if any(x in p.parts for x in ('#recycle','临时','.dedup-quarantine')):continue
            m=re.fullmatch(r'(视频|图文)-(\d{15,22})\.mp4',p.name)
            if not m or m[2] not in known or int(size)<1024:continue
            root_key=next((key for key,prefix in root_prefixes.items() if p.is_relative_to(Path(prefix))),None)
            if root_key is None:continue
            relative=str(p.relative_to(Path(root_prefixes[root_key])))
            db.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?)',(m[2],root_key,relative,int(size),'note' if m[1]=='图文' else 'video'))
            count+=1
    return count


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--source',action='append',type=Path,default=[])
    parser.add_argument('--inventory',type=Path)
    parser.add_argument('--root-prefixes',type=Path)
    args=parser.parse_args(argv)
    store=LibraryStore(args.root)
    if args.source:print(json.dumps(import_sources(store,args.source),ensure_ascii=False),flush=True)
    if args.inventory:print('existing candidates',import_candidates(store,args.inventory,json.loads(args.root_prefixes.read_text())),flush=True)
    with store.connect() as db:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')


if __name__=='__main__':main()
