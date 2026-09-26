"""Offline, no-overwrite audio recovery. Stop both bots first. Supports backup v1/v2.
Usage: python scripts/restore_audio_backup.py BACKUP.zip --data-dir /data [--apply]
Without --apply, this only validates the backup and prints what would be restored.
Metadata/accounts are NEVER imported by this script.
"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import re
import tempfile
import zipfile


def restore(archive_path, data_dir, apply=False):
    root = Path(data_dir).resolve() / 'uploads'
    count = 0
    with zipfile.ZipFile(archive_path) as archive:
        info = json.loads(archive.read('backup-info.json'))
        if info.get('format') != 'appearance-music-backup' or info.get('version') not in {1, 2}:
            raise ValueError('지원하는 APPEARANCE 백업이 아닙니다.')
        plans = []
        for aid in info.get('includedAssetIds', []):
            if not re.fullmatch('[a-f0-9]{32}', aid): raise ValueError('잘못된 파일 ID')
            record = info.get('assets', {}).get(aid, {})
            meta_path = record.get('metadataPath', f'uploads/{aid}.json')
            meta = json.loads(archive.read(meta_path))
            stored = meta.get('storedName', '')
            if not re.fullmatch(aid + r'\.(mp3|wav|ogg|m4a|flac|aac|opus|webm)', stored):
                raise ValueError('잘못된 서버 파일명')
            if meta.get('id') != aid: raise ValueError('메타데이터 ID 불일치')
            member = record.get('archivePath', 'uploads/' + stored)
            for name in (meta_path, member):
                if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name:
                    raise ValueError('안전하지 않은 백업 경로')
            digest = hashlib.sha256()
            size = 0
            with archive.open(member) as source:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > int(meta['bytes']): raise ValueError('파일 크기 불일치')
                    digest.update(chunk)
            if size != int(meta['bytes']) or (meta.get('sha256') and digest.hexdigest() != meta['sha256']):
                raise ValueError('오디오 무결성 검사 실패: ' + str(meta.get('name')))
            target = root / stored
            meta_target = root / (aid + '.json')
            if target.exists():
                old_digest = hashlib.sha256()
                with target.open('rb') as f:
                    while chunk := f.read(1024 * 1024): old_digest.update(chunk)
                if old_digest.hexdigest() != digest.hexdigest(): raise ValueError('서버에 다른 파일이 이미 있습니다: ' + stored)
            if meta_target.exists() and json.loads(meta_target.read_text(encoding='utf-8')) != meta:
                raise ValueError('서버 메타데이터와 충돌합니다: ' + aid)
            plans.append((member, target, meta_target, meta))
        if apply: root.mkdir(parents=True, exist_ok=True)
        for member, target, meta_target, meta in plans:
            print(('복원: ' if apply else '검사 완료: ') + str(meta.get('name')))
            if apply:
                if not target.exists():
                    fd, temp = tempfile.mkstemp(dir=root, prefix='.restore-')
                    try:
                        with os.fdopen(fd, 'wb') as out, archive.open(member) as source:
                            while chunk := source.read(1024 * 1024): out.write(chunk)
                            out.flush(); os.fsync(out.fileno())
                        # Hard link publishes atomically and refuses overwrite.
                        os.link(temp, target)
                    finally:
                        Path(temp).unlink(missing_ok=True)
                if not meta_target.exists():
                    with meta_target.open('x', encoding='utf-8') as f:
                        json.dump(meta, f, ensure_ascii=False)
                        f.flush(); os.fsync(f.fileno())
            count += 1
    return count

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('backup'); p.add_argument('--data-dir', required=True)
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    print('처리 파일 수:', restore(a.backup, a.data_dir, a.apply))
