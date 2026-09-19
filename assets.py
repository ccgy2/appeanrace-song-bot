"""업로드 파일 저장 및 로컬 사운드 관리. 사용자 파일명을 경로로 사용하지 않는다."""
from __future__ import annotations
import asyncio
import json
import math
import os
from pathlib import Path
import re
import subprocess
import uuid
from config import ROOT, Settings
from validation import AUDIO_EXTENSIONS, EVENTS


class Assets:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir / 'uploads'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()

    def get(self, asset_id: str) -> dict:
        if not re.fullmatch(r'[a-f0-9]{32}', asset_id):
            raise ValueError('잘못된 파일 ID입니다.')
        meta = self.root / (asset_id + '.json')
        if not meta.is_file():
            raise ValueError('업로드 파일이 없습니다. 영구 저장소를 확인하거나 다시 업로드하세요.')
        d = json.loads(meta.read_text(encoding='utf-8'))
        path = (self.root / d['storedName']).resolve()
        if path.parent != self.root.resolve() or not path.is_file():
            raise ValueError('오디오 파일이 없거나 경로가 잘못됐습니다.')
        return {**d, 'path': path}

    def _probe(self, path: Path) -> float:
        try:
            result = subprocess.run(
                [self.settings.ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                 '-select_streams', 'a:0', '-show_entries', 'stream=codec_type:format=duration',
                 '-of', 'json', str(path)], capture_output=True, timeout=20, check=True,
            )
            info = json.loads(result.stdout)
            duration = float(info.get('format', {}).get('duration', 0))
            if not info.get('streams') or not math.isfinite(duration) or duration <= 0 or duration > 86400:
                raise ValueError
            return round(duration, 3)
        except FileNotFoundError:
            raise ValueError('ffprobe가 없습니다. FFmpeg 설치 또는 Docker 배포를 확인하세요.') from None
        except (ValueError, KeyError, subprocess.SubprocessError):
            raise ValueError('정상적인 오디오 파일이 아니거나 검사 시간이 초과됐습니다.') from None

    async def upload(self, part) -> dict:
        name = (part.filename or '').replace('\\', '/').split('/')[-1][:180]
        ext = Path(name).suffix.lower()
        if ext not in AUDIO_EXTENSIONS:
            raise ValueError('MP3, WAV, OGG, M4A, FLAC, AAC, OPUS, WEBM 파일만 등록할 수 있습니다.')
        async with self.lock:
            used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
            limit = self.settings.max_upload_mb * 1024 * 1024
            remaining = self.settings.max_storage_mb * 1024 * 1024 - used
            asset_id = uuid.uuid4().hex
            path = self.root / (asset_id + ext)
            meta = self.root / (asset_id + '.json')
            size = 0
            try:
                with path.open('xb') as f:
                    while chunk := await part.read_chunk(size=64 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise ValueError(f'파일 하나당 최대 {self.settings.max_upload_mb}MB입니다.')
                        if size > remaining:
                            raise ValueError('업로드 저장 공간이 부족합니다. 관리자가 DATA_DIR 공간을 정리해야 합니다.')
                        await asyncio.to_thread(f.write, chunk)
                duration = await asyncio.to_thread(self._probe, path)
                result = {'id': asset_id, 'name': name, 'storedName': path.name, 'bytes': size, 'duration': duration}
                temp = meta.with_suffix('.json.tmp')
                temp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
                os.replace(temp, meta)
                return {k: v for k, v in result.items() if k != 'storedName'}
            except BaseException:
                path.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                meta.with_suffix('.json.tmp').unlink(missing_ok=True)
                raise

    def bundled(self, key: str) -> list[Path]:
        if key not in EVENTS:
            raise ValueError('알 수 없는 효과음입니다.')
        root = ROOT / 'sounds' / key
        return sorted(p for p in root.glob('*') if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS)

    def legacy_file(self, key: str, filename: str) -> Path:
        root = (ROOT / 'sounds').resolve()
        relative = Path(str(filename))
        if relative.is_absolute() or '..' in relative.parts or '\\' in str(filename):
            raise ValueError('효과음 파일 경로가 잘못됐습니다.')
        for candidate in (root / relative, root / key / relative):
            path = candidate.resolve()
            if path.is_relative_to(root) and path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                return path
        raise ValueError('등록된 효과음 파일을 찾을 수 없습니다.')
