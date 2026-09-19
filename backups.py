"""Private ZIP backups: music metadata and uploaded audio only, never credentials/accounts."""
from __future__ import annotations
import json
import re
import time
import zipfile


def write_music_backup(handle, assets, metadata):
    # Audio is already compressed. ZIP_STORED keeps CPU use and voice latency low.
    included, skipped = [], []
    with zipfile.ZipFile(handle, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr('metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
        for meta in sorted(assets.root.glob('*.json')):
            if not re.fullmatch(r'[a-f0-9]{32}', meta.stem):
                continue
            try:
                asset = assets.get(meta.stem)
            except (ValueError, OSError):
                skipped.append(meta.stem)
                continue
            archive.write(asset['path'], 'uploads/' + asset['path'].name)
            archive.write(meta, 'uploads/' + meta.name)
            included.append(meta.stem)
        referenced = set()
        for team in metadata.get('teams', {}).values():
            for songs in team.get('library', {}).values():
                referenced.update(song['assetId'] for song in songs if song.get('assetId'))
            referenced.update(event['assetId'] for event in team.get('events', []) if event.get('assetId'))
        info = {'format': 'appearance-music-backup', 'version': 1, 'createdAt': int(time.time()),
                'includedAssetIds': included, 'missingAssetIds': sorted(referenced - set(included)),
                'invalidAssetIds': skipped,
                'note': '이미 없어진 파일은 복구되지 않습니다. 계정, 비밀번호, Firebase 키, Discord 토큰, 기본 sounds는 포함하지 않습니다.'}
        archive.writestr('backup-info.json', json.dumps(info, ensure_ascii=False, indent=2))
    handle.seek(0)
