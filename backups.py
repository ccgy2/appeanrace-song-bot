"""Private backups with original audio names and an explicit ID-to-ZIP-path restore map."""
from __future__ import annotations
import json
from pathlib import PurePosixPath
import re
import time
import unicodedata
import zipfile


def original_filename(name: str, fallback: str) -> str:
    # ZIP names must be safe to extract on Windows/macOS/Linux. Normal names are unchanged.
    name = unicodedata.normalize('NFC', str(name or '').replace('\\', '/').split('/')[-1])
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', name).strip().rstrip('.')
    if not name or name in {'.', '..'}:
        name = '오디오' + fallback
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name):
        name = '_' + name
    return name


def unique_filename(name: str, used: set[str]) -> str:
    path = PurePosixPath(name)
    stem, ext = path.stem, path.suffix
    result, index = name, 2
    while result.casefold() in used:
        result = f'{stem} ({index}){ext}'
        index += 1
    used.add(result.casefold())
    return result


def write_music_backup(handle, assets, metadata):
    included, skipped, mapping, used = [], [], {}, set()
    with zipfile.ZipFile(handle, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr('metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
        for meta in sorted(assets.root.glob('*.json')):
            if not re.fullmatch(r'[a-f0-9]{32}', meta.stem): continue
            try:
                asset = assets.get(meta.stem)
            except (ValueError, OSError):
                skipped.append(meta.stem)
                continue
            name = unique_filename(original_filename(asset.get('name'), asset['path'].suffix), used)
            audio_path = 'uploads/' + name
            metadata_path = 'asset-metadata/' + meta.name
            archive.write(asset['path'], audio_path)
            archive.write(meta, metadata_path)
            included.append(meta.stem)
            mapping[meta.stem] = {'archivePath': audio_path, 'metadataPath': metadata_path,
                                  'originalName': asset.get('name'), 'storedName': asset['path'].name,
                                  'sha256': asset.get('sha256'), 'bytes': asset.get('bytes')}
        referenced = set()
        for team in metadata.get('teams', {}).values():
            libraries = team.get('library') or {'entrance': team.get('songs', [])}
            for songs in libraries.values():
                referenced.update(s['assetId'] for s in songs if s.get('assetId'))
            for event in team.get('events', []):
                if event.get('assetId'): referenced.add(event['assetId'])
                referenced.update(t['assetId'] for t in event.get('tracks', [])
                                  if isinstance(t, dict) and t.get('assetId'))
        info = {'format': 'appearance-music-backup', 'version': 2, 'createdAt': int(time.time()),
                'includedAssetIds': included, 'missingAssetIds': sorted(referenced - set(included)),
                'invalidAssetIds': skipped, 'assets': mapping,
                'note': 'uploads/ 안의 오디오는 원래 등록 파일명입니다. 이름이 겹치면 (2), (3)을 붙입니다. 서버 복원 경로는 assets 매핑을 사용하세요. 계정/비밀번호/토큰/기본 sounds/YouTube 오디오 자체는 포함하지 않습니다.'}
        archive.writestr('backup-info.json', json.dumps(info, ensure_ascii=False, indent=2))
        archive.writestr('백업_안내.txt', 'uploads 폴더: 원래 등록한 이름의 오디오\nasset-metadata 폴더: 내부 파일 ID와 원래 이름 정보\nmetadata.json: 곡·타순·경기 상황·자동 등장곡 설정\nbackup-info.json: 복원 경로 매핑과 누락 파일 목록\n\n오디오 복원: 봇 중지 후 scripts/restore_audio_backup.py 사용. 이 도구는 계정이나 음악 DB를 변경하지 않습니다.\nYouTube 링크만 등록한 곡은 링크 정보만 저장되며 오디오 파일은 포함되지 않습니다.\n')
    handle.seek(0)
