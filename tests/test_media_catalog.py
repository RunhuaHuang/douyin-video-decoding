import hashlib
import json

import pandas as pd
import pytest

import batch_download_huitun as batch
from app.library_downloader import existing_media
from app.media_catalog import CATALOG_NAME, CatalogLookupError, locate_media

WID = '7667889682693290106'


def setup_catalog(tmp_path, *, primary=False):
    old4, old5 = tmp_path/'old4', tmp_path/'old5'
    old4.mkdir(); old5.mkdir()
    out = old5/'表'; out.mkdir()
    target = old4/f'图文-{WID}.mp4'
    target.write_bytes(b'complete media' * 1200)
    ref = {'work_id': WID, 'storage_root': 'old4', 'relative_path': target.name,
           'kind': 'note', 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    data = {'version': 1, 'local_root': 'old5', 'roots': {'old4': [str(old4)], 'old5': [str(old5)]},
            'aliases': {'old5\0表/'+target.name: ref}, 'primaries': {WID: ref} if primary else {}}
    (old5/CATALOG_NAME).write_text(json.dumps(data))
    return out, target, ref


def test_cross_root_alias_preserves_old_lookup_without_remote(tmp_path):
    out, target, _ = setup_catalog(tmp_path)
    assert locate_media(out, WID)['path'] == target
    assert existing_media(out, WID) == target
    assert locate_media(out, '999') is None


@pytest.mark.parametrize('problem', ['missing', 'changed', 'unmounted'])
def test_known_media_fails_closed_instead_of_redownloading(tmp_path, problem):
    out, target, _ = setup_catalog(tmp_path)
    assert locate_media(out, WID)
    if problem == 'missing':
        target.unlink()
    elif problem == 'changed':
        target.write_bytes(b'x' * target.stat().st_size)
    else:
        target.parent.rename(tmp_path/'unmounted')
    with pytest.raises(CatalogLookupError):
        existing_media(out, WID)


@pytest.mark.parametrize('primary', [False, True])
def test_real_excel_resume_never_calls_remote_for_catalogued_media(tmp_path, monkeypatch, primary):
    out, target, _ = setup_catalog(tmp_path, primary=primary)
    if primary:
        (out/target.name).write_bytes(b'old incomplete variant' * 1000)
    source = tmp_path/'表.xlsx'
    pd.DataFrame({'播放地址': [f'https://www.douyin.com/note/{WID}']}).to_excel(source, index=False)
    worker = batch.TableWorker(1, out.parent, tmp_path/'guest.json')
    monkeypatch.setattr(worker, 'resolve_video_info', lambda *a, **kw: pytest.fail('remote parse prohibited'))
    monkeypatch.setattr(worker, 'synthesize_note_mp4', lambda *a, **kw: pytest.fail('remote download prohibited'))
    try:
        worker.process_excel(source)
        row = json.loads((out/'manifest.json').read_text())[WID]
        assert row['status'] == 'success'
        assert row['file'] == str(target)
        assert row['asset_ref']['sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    finally:
        worker.close()


def test_real_excel_resume_missing_primary_records_failure_without_remote(tmp_path, monkeypatch):
    out, target, _ = setup_catalog(tmp_path, primary=True)
    target.unlink()
    source = tmp_path/'表.xlsx'
    pd.DataFrame({'播放地址': [f'https://www.douyin.com/note/{WID}']}).to_excel(source, index=False)
    worker = batch.TableWorker(1, out.parent, tmp_path/'guest.json')
    monkeypatch.setattr(worker, 'resolve_video_info', lambda *a, **kw: pytest.fail('remote parse prohibited'))
    try:
        worker.process_excel(source)
        row = json.loads((out/'manifest.json').read_text())[WID]
        assert row['status'] == 'failed' and '不重新下载' in row['reason']
    finally:
        worker.close()


def test_corrupt_catalogue_and_traversal_fail_closed(tmp_path):
    out, target, _ = setup_catalog(tmp_path)
    catalog = out.parent/CATALOG_NAME
    data = json.loads(catalog.read_text())
    next(iter(data['aliases'].values()))['relative_path'] = '../outside.mp4'
    catalog.write_text(json.dumps(data))
    with pytest.raises(CatalogLookupError):
        locate_media(out, WID)
    catalog.write_text('{broken')
    with pytest.raises(CatalogLookupError):
        locate_media(out, WID)


def test_quarantined_duplicates_are_not_reimported_as_candidates(tmp_path):
    from app.library_store import LibraryStore
    from app.library_import import import_candidates
    store = LibraryStore(tmp_path/'library')
    with store.transaction() as db:
        db.execute('INSERT INTO works(work_id,url,assigned_worker,priority,updated_at) VALUES(?,?,?,?,?)',
                   (WID, 'https://www.douyin.com/note/'+WID, 'mac', 0, 0))
    inventory = tmp_path/'inventory.txt'
    inventory.write_text(f'12000\t/old4/.dedup-quarantine/batch/图文-{WID}.mp4\n'
                         f'12000\t/old4/kept/图文-{WID}.mp4\n')
    assert import_candidates(store, inventory, {'old4': '/old4'}) == 1
    with store.connect() as db:
        assert db.execute('SELECT relative_path FROM candidates').fetchone()[0] == f'kept/图文-{WID}.mp4'
