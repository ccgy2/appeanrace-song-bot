"""업로드 파일 저장 및 로컬 사운드 관리. 사용자 파일명을 경로로 사용하지 않는다."""
from __future__ import annotations
import asyncio
import json
import hashlib
import shutil
import time
import math
import os
from pathlib import Path
import re
import subprocess
import uuid
from config import ROOT, Settings
from validation import AUDIO_EXTENSIONS, EVENTS
from persistence import require_upload_storage


class Assets:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir / 'uploads'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()

    @staticmethod
    def upload_name(filename: str) -> tuple[str, str]:
        name = str(filename or '').replace('\\', '/').split('/')[-1]
        ext = Path(name).suffix.lower()
        if ext not in AUDIO_EXTENSIONS or any(ord(c) < 32 for c in name):
            raise ValueError('MP3, WAV, OGG, M4A, FLAC, AAC, OPUS, WEBM, MP4 파일만 등록할 수 있습니다.')
        # Preserve the extension even when an original filename is very long.
        name = name[:180 - len(ext)] + ext if len(name) > 180 else name
        return name, ext

    @staticmethod
    def public_metadata(value: dict) -> dict:
        return {k: value[k] for k in ('id', 'name', 'bytes', 'duration', 'sha256', 'createdAt') if k in value}

    @staticmethod
    def sync_directory(path: Path):
        if os.name == 'posix':
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def remaining_upload_bytes(self, exclude_session: str = '') -> int:
        """Include full reservations, so legacy and resumable uploads share a quota.

        Free disk already excludes the bytes of existing partial files; only
        their still-unwritten bytes must be reserved a second time here.
        Caller holds self.lock while allocating a new upload.
        """
        used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        reserved = pending = 0
        sessions = self.settings.data_dir / 'upload-sessions'
        if sessions.is_dir():
            for path in sessions.glob('*.json'):
                if path.stem == exclude_session or not re.fullmatch(r'[a-f0-9]{32}', path.stem):
                    continue
                try:
                    if path.stat().st_size > 65536:
                        continue
                    item = json.loads(path.read_text(encoding='utf-8'))
                    if item.get('status') not in {'uploading', 'processing'}:
                        continue
                    size = int(item['size'])
                    if size <= 0:
                        continue
                    # A crash after publishing asset metadata but before updating
                    # its session must not count the same durable asset twice.
                    if (self.root / (path.stem + '.json')).is_file():
                        continue
                    reserved += size
                    partial = sessions / (path.stem + '.part')
                    actual = partial.stat().st_size if partial.is_file() else 0
                    if not actual:
                        moved = self.root / (path.stem + str(item.get('extension', '')))
                        if moved.parent == self.root and moved.is_file():
                            actual = moved.stat().st_size
                            # Already included in used, although its metadata is
                            # not yet published after an interrupted finalization.
                            reserved -= actual
                    pending += max(0, size - actual)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
        return min(self.settings.max_storage_mb * 1024 * 1024 - used - reserved,
                   shutil.disk_usage(self.root).free - pending - 8 * 1024 * 1024)

    def inspect_staged(self, path: Path, expected_size: int, stop=None) -> dict:
        """Called in a worker thread. Never read an entire large asset in memory."""
        if path.stat().st_size != expected_size:
            raise ValueError('업로드된 파일 크기가 일치하지 않습니다. 다시 업로드하세요.')
        if stop is not None and stop.is_set():
            raise InterruptedError('파일 검사가 중단됐습니다.')
        duration = self._probe(path)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                if stop is not None and stop.is_set():
                    raise InterruptedError('파일 검사가 중단됐습니다.')
                digest.update(chunk)
        return {'duration': duration, 'sha256': digest.hexdigest()}

    def commit_staged(self, asset_id: str, name: str, stage: Path, size: int, inspected: dict) -> dict:
        """Publish a checked file by same-volume rename; never copy the payload.

        The payload is moved before its atomic metadata write. A resumable
        session can recover either intermediate state following a restart.
        Caller holds self.lock; all work runs outside the event loop.
        """
        if not re.fullmatch(r'[a-f0-9]{32}', asset_id):
            raise ValueError('잘못된 파일 ID입니다.')
        name, extension = self.upload_name(name)
        target = self.root / (asset_id + extension)
        metadata = self.root / (asset_id + '.json')
        if metadata.exists():
            existing = self.get(asset_id)
            if existing['bytes'] != size or existing.get('sha256') != inspected['sha256']:
                raise ValueError('이미 저장된 파일 정보가 일치하지 않습니다.')
            return self.public_metadata(existing)
        if stage.stat().st_size != size:
            raise ValueError('업로드된 파일 크기가 일치하지 않습니다.')
        if stage.resolve() != target.resolve():
            if stage.stat().st_dev != self.root.stat().st_dev:
                raise ValueError('업로드 임시 폴더와 파일 저장소가 같은 영구 볼륨에 있어야 합니다.')
            if target.exists():
                raise ValueError('파일 저장 위치가 이미 사용 중입니다. 업로드 상태를 다시 확인하세요.')
            os.replace(stage, target)
            self.sync_directory(stage.parent)
            self.sync_directory(self.root)
        result = {'id': asset_id, 'name': name, 'storedName': target.name,
                  'bytes': size, 'duration': inspected['duration'], 'sha256': inspected['sha256'],
                  'createdAt': int(time.time())}
        temp = metadata.with_suffix('.json.tmp')
        try:
            with temp.open('w', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, metadata)
            self.sync_directory(self.root)
        finally:
            temp.unlink(missing_ok=True)
        return self.public_metadata(result)

    def get(self, asset_id: str) -> dict:
        if not re.fullmatch(r'[a-f0-9]{32}', asset_id):
            raise ValueError('잘못된 파일 ID입니다.')
        meta = self.root / (asset_id + '.json')
        if not meta.is_file():
            raise ValueError('업로드 파일이 없습니다. 영구 저장소를 확인하거나 다시 업로드하세요.')
        try:
            d = json.loads(meta.read_text(encoding='utf-8'))
            if not isinstance(d, dict) or d.get('id') != asset_id:
                raise ValueError
            stored = d['storedName']
            if not isinstance(stored, str) or Path(stored).stem != asset_id or Path(stored).suffix.lower() not in AUDIO_EXTENSIONS:
                raise ValueError
            duration = float(d['duration'])
            if not math.isfinite(duration) or not 0 < duration <= 86400:
                raise ValueError
            path = (self.root / stored).resolve()
            if path.parent != self.root.resolve() or not path.is_file():
                raise ValueError
            if path.stat().st_size != int(d['bytes']):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('오디오 파일이 없거나 손상됐습니다. 볼륨 경로/백업을 확인하거나 수정에서 원본을 다시 업로드하세요.') from None
        return {**d, 'path': path, 'duration': duration}

    def _probe(self, path: Path) -> float:
        try:
            result = subprocess.run(
                [self.settings.ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                 '-select_streams', 'a:0', '-show_entries', 'stream=codec_type:format=duration',
                 '-of', 'json', str(path)], capture_output=True, timeout=20, check=True,
            )
            info = json.loads(result.stdout)
            duration = float(info.get('format', {}).get('duration', 0))
            if (not info.get('streams') or not any(s.get('codec_type') == 'audio' for s in info['streams'])
                    or not math.isfinite(duration) or duration <= 0 or duration > 86400):
                raise ValueError
            return round(duration, 3)
        except FileNotFoundError:
            raise ValueError('ffprobe가 없습니다. FFmpeg 설치 또는 Docker 배포를 확인하세요.') from None
        except (ValueError, KeyError, subprocess.SubprocessError):
            raise ValueError('재생 가능한 오디오가 없거나 검사 시간이 초과됐습니다. MP4에는 오디오 트랙이 있어야 합니다.') from None

    async def upload(self, part) -> dict:
        require_upload_storage(self.settings)
        name, ext = self.upload_name(part.filename)
        async with self.lock:
            limit = self.settings.max_upload_mb * 1024 * 1024
            remaining = await asyncio.to_thread(self.remaining_upload_bytes)
            asset_id = uuid.uuid4().hex
            path = self.root / (asset_id + ext)
            meta = self.root / (asset_id + '.json')
            size = 0
            digest = hashlib.sha256()
            try:
                with path.open('xb') as f:
                    while chunk := await part.read_chunk(size=64 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise ValueError(f'파일 하나당 최대 {self.settings.max_upload_mb}MB입니다.')
                        if size > remaining:
                            raise ValueError('업로드 저장 공간이 부족합니다. 관리자가 DATA_DIR 공간을 정리해야 합니다.')
                        digest.update(chunk)
                        await asyncio.to_thread(f.write, chunk)
                    await asyncio.to_thread(f.flush)
                    await asyncio.to_thread(os.fsync, f.fileno())
                duration = await asyncio.to_thread(self._probe, path)
                result = {'id': asset_id, 'name': name, 'storedName': path.name, 'bytes': size, 'duration': duration, 'sha256': digest.hexdigest(), 'createdAt': int(time.time())}
                temp = meta.with_suffix('.json.tmp')
                with temp.open('w', encoding='utf-8') as f:
                    f.write(json.dumps(result, ensure_ascii=False))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp, meta)
                if os.name == 'posix':
                    fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
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
