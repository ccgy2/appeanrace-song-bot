"""Durable, owner-scoped uploads in bounded chunks on the existing data volume.

One application process owns a volume, matching the existing SQLite deployment.
The checkpoint is the only acknowledged offset. An interrupted chunk is removed
before its session is resumed. Finalization never copies the uploaded payload.
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
import weakref

from assets import Assets
from persistence import require_upload_storage


CHUNK_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024
TTL_SECONDS = 24 * 60 * 60
MAX_ACTIVE_SESSIONS = 8
MAX_OWNER_SESSIONS = 3
MAX_SESSION_RECORDS = 1024
DISK_HEADROOM = 8 * 1024 * 1024
_ID = re.compile(r'^[a-f0-9]{32}$')
_STATUSES = {'uploading', 'processing', 'complete', 'error'}


async def _io(fn, *args):
    """Do not close/delete a file while its off-loop write is still running."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


def _storage_error(exc: OSError) -> ValueError:
    if exc.errno in {errno.ENOSPC, getattr(errno, 'EDQUOT', -1)}:
        return ValueError('업로드 저장 공간이 부족합니다. 영구 볼륨의 여유 공간을 확인한 뒤 다시 시도하세요.')
    return ValueError('업로드 파일을 저장하지 못했습니다. 저장소 상태를 확인한 뒤 이어서 올리기를 다시 시도하세요.')


class UploadSessions:
    def __init__(self, assets: Assets):
        self.assets = assets
        self.root = assets.settings.data_dir / 'upload-sessions'
        self._locks = weakref.WeakValueDictionary()
        self._tasks: dict[str, asyncio.Task] = {}
        self._stops: dict[str, threading.Event] = {}
        self._busy: set[str] = set()
        self._processing = asyncio.Semaphore(1)
        self._closed = False
        self._last_cleanup = 0.0

    def _check_open(self):
        if self._closed:
            raise ValueError('서버가 재시작 중입니다. 잠시 뒤 이어서 올리기를 다시 시도하세요.')

    @staticmethod
    def _owner(owner) -> str:
        if not isinstance(owner, str) or not 1 <= len(owner) <= 128 or any(ord(c) < 32 for c in owner):
            raise PermissionError('로그인한 계정으로 다시 시도하세요.')
        return owner

    @staticmethod
    def _id(ident) -> str:
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            raise ValueError('잘못된 업로드 ID입니다.')
        return ident

    def _lock(self, ident):
        return self._locks.setdefault(ident, asyncio.Lock())

    def _manifest(self, ident):
        return self.root / (ident + '.json')

    def _part(self, ident):
        return self.root / (ident + '.part')

    def _target(self, item):
        return self.assets.root / (item['id'] + item['extension'])

    def _read(self, ident: str, owner: str | None = None) -> dict:
        path = self._manifest(ident)
        try:
            if not path.is_file():
                raise FileNotFoundError('업로드가 만료됐거나 취소됐습니다. 파일을 다시 선택하세요.')
            if path.stat().st_size > 65536:
                raise ValueError
            item = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(item, dict) or item.get('version') != 1 or item.get('id') != ident
                    or item.get('status') not in _STATUSES or not isinstance(item.get('owner'), str)
                    or type(item.get('size')) is not int or not 0 < item['size'] <= MAX_UPLOAD_BYTES
                    or type(item.get('offset')) is not int or not 0 <= item['offset'] <= item['size']
                    or not isinstance(item.get('name'), str)
                    or self.assets.upload_name(item['name'])[1] != item.get('extension')
                    or not isinstance(item.get('updatedAt'), (int, float))):
                raise ValueError
        except FileNotFoundError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise ValueError('업로드 상태 파일을 읽지 못했습니다. 파일을 다시 선택하세요.') from None
        if owner is not None and item['owner'] != owner:
            raise PermissionError('이 업로드는 시작한 계정으로만 확인하거나 변경할 수 있습니다.')
        return item

    def _write(self, item):
        path = self._manifest(item['id'])
        temp = path.with_suffix('.json.tmp')
        try:
            with temp.open('w', encoding='utf-8') as stream:
                json.dump(item, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            self.assets.sync_directory(self.root)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _public(item):
        value = {k: item[k] for k in ('id', 'status', 'offset', 'size')}
        value['chunkSize'] = CHUNK_BYTES
        if item.get('error'):
            value['error'] = str(item['error'])[:300]
        if item.get('result'):
            value['result'] = Assets.public_metadata(item['result'])
        return value

    def _existing_result(self, item):
        try:
            found = self.assets.get(item['id'])
            if found['bytes'] == item['size'] and found['name'] == item['name']:
                return self.assets.public_metadata(found)
        except (ValueError, OSError):
            pass
        return None

    def _remove_incomplete(self, item):
        self._part(item['id']).unlink(missing_ok=True)
        # A completed asset is user data, even if its final session response was
        # lost. Cancellation and expiry must never delete that saved asset.
        if not self._existing_result(item):
            self._target(item).unlink(missing_ok=True)
            metadata = self.assets.root / (item['id'] + '.json')
            metadata.unlink(missing_ok=True)
            metadata.with_suffix('.json.tmp').unlink(missing_ok=True)

    def _truncate(self, path, offset):
        with path.open('r+b') as stream:
            stream.truncate(offset)
            stream.flush()
            os.fsync(stream.fileno())

    def _recover(self, item):
        """Reconcile the two atomic records after any interrupted operation."""
        if item['status'] in {'processing', 'complete'}:
            result = self._existing_result(item)
            if result:
                if item['status'] != 'complete' or item.get('result') != result:
                    item = {**item, 'status': 'complete', 'result': result,
                            'offset': item['size'], 'updatedAt': time.time()}
                    item.pop('error', None)
                    # Asset metadata itself is a durable completion receipt.
                    # A full disk must not make a saved asset look unsaved.
                    try:
                        self._write(item)
                    except OSError:
                        pass
                return item
            if item['status'] == 'complete':
                return {**item, 'status': 'error', 'error': '완료된 파일을 찾을 수 없습니다. 영구 저장소를 확인하세요.', 'result': None}
        if item['status'] not in {'uploading', 'processing'}:
            return item
        path = self._part(item['id'])
        if item['status'] == 'processing' and not path.exists():
            # Crash between payload rename and metadata publication.
            path = self._target(item)
        if not path.is_file() or path.stat().st_size < item['offset']:
            item = {**item, 'status': 'error', 'error': '임시 업로드 파일이 없거나 손상됐습니다. 파일을 다시 선택하세요.', 'updatedAt': time.time()}
            self._remove_incomplete(item)
            self._write(item)
            return item
        if path.stat().st_size > item['offset']:
            self._truncate(path, item['offset'])
        return item

    def _load(self, ident, owner):
        return self._recover(self._read(ident, owner))

    def _cleanup(self, protected):
        if not self.root.is_dir():
            return
        now = time.time()
        for path in self.root.glob('*.json'):
            ident = path.stem
            if not _ID.fullmatch(ident) or ident in protected:
                continue
            try:
                item = self._read(ident)
                expired = now - item['updatedAt'] >= TTL_SECONDS
            except (ValueError, OSError):
                item = None
                expired = now - path.stat().st_mtime >= TTL_SECONDS
            if not expired:
                continue
            if item:
                self._remove_incomplete(item)
            else:
                self._part(ident).unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            path.with_suffix('.json.tmp').unlink(missing_ok=True)
        # An initial power loss can leave a part before its first manifest.
        for path in self.root.iterdir():
            ident = path.name.split('.', 1)[0]
            if (not _ID.fullmatch(ident) or ident in protected
                    or path.suffix not in {'.part', '.tmp'} or self._manifest(ident).exists()):
                continue
            if now - path.stat().st_mtime >= TTL_SECONDS:
                path.unlink(missing_ok=True)

    async def _maintenance(self, force=False):
        if not force and time.monotonic() - self._last_cleanup < 60:
            return
        async with self.assets.lock:
            try:
                await _io(self._cleanup, set(self._tasks) | set(self._busy))
            except OSError as exc:
                raise _storage_error(exc) from None
            self._last_cleanup = time.monotonic()

    def _start(self, owner, filename, size, request_id=None):
        name, extension = self.assets.upload_name(filename)
        limit = min(MAX_UPLOAD_BYTES, self.assets.settings.max_upload_mb * 1024 * 1024)
        if type(size) is not int or not 0 < size <= MAX_UPLOAD_BYTES:
            raise ValueError(f'파일 하나당 최대 {limit // (1024 * 1024)}MB이며 빈 파일은 올릴 수 없습니다.')
        self.root.mkdir(parents=True, exist_ok=True)
        records = list(self.root.glob('*.json'))
        # An interrupted create response must not reserve the same large file
        # again. Only an explicit operation key is reusable; matching filenames
        # and sizes alone never identify an upload or another account's data.
        if request_id is not None:
            for path in records:
                if not _ID.fullmatch(path.stem):
                    continue
                try:
                    existing = self._read(path.stem)
                except (ValueError, FileNotFoundError):
                    continue
                if existing['owner'] == owner and existing.get('requestId') == request_id:
                    if existing['name'] != name or existing['size'] != size:
                        raise ValueError('같은 업로드 요청 ID에 다른 파일을 사용할 수 없습니다. 새 업로드를 시작하세요.')
                    return self._recover(existing)
        if size > limit:
            raise ValueError(f'파일 하나당 최대 {limit // (1024 * 1024)}MB입니다.')
        if len(records) >= MAX_SESSION_RECORDS:
            raise ValueError('보관 중인 업로드 상태가 많습니다. 만료된 업로드가 정리된 뒤 다시 시도하세요.')
        active = []
        for path in records:
            if not _ID.fullmatch(path.stem):
                continue
            try:
                item = self._read(path.stem)
                if item['status'] in {'uploading', 'processing'} and not self._existing_result(item):
                    active.append(item)
            except ValueError:
                continue
        if len(active) >= MAX_ACTIVE_SESSIONS or sum(item['owner'] == owner for item in active) >= MAX_OWNER_SESSIONS:
            raise ValueError('진행 중인 업로드가 많습니다. 기존 업로드를 완료하거나 취소한 뒤 다시 시도하세요.')
        if size > self.assets.remaining_upload_bytes():
            raise ValueError('업로드 저장 공간이 부족합니다. 진행 중인 파일과 영구 볼륨 용량을 확인하세요.')
        ident, now = uuid.uuid4().hex, time.time()
        item = {'version': 1, 'id': ident, 'owner': owner, 'name': name, 'extension': extension,
                'size': size, 'offset': 0, 'status': 'uploading', 'createdAt': now, 'updatedAt': now}
        if request_id is not None:
            item['requestId'] = request_id
        try:
            with self._part(ident).open('xb') as stream:
                stream.flush()
                os.fsync(stream.fileno())
            self._write(item)
        except BaseException:
            self._part(ident).unlink(missing_ok=True)
            self._manifest(ident).unlink(missing_ok=True)
            raise
        return item

    async def start(self, owner, filename, size, request_id=None):
        self._check_open()
        owner = self._owner(owner)
        if request_id is not None and (not isinstance(request_id, str) or not _ID.fullmatch(request_id)):
            raise ValueError('업로드 요청 ID는 32자리 영문 소문자·숫자 형식이어야 합니다.')
        require_upload_storage(self.assets.settings)
        await self._maintenance(force=True)
        async with self.assets.lock:
            try:
                item = await _io(self._start, owner, filename, size, request_id)
                if item['status'] == 'processing':
                    self._schedule(item)
                return self._public(item)
            except OSError as exc:
                raise _storage_error(exc) from None

    async def status(self, owner, ident):
        self._check_open()
        owner, ident = self._owner(owner), self._id(ident)
        await self._maintenance()
        async with self._lock(ident):
            try:
                item = await _io(self._load, ident, owner)
            except (FileNotFoundError, PermissionError):
                raise
            except OSError as exc:
                raise _storage_error(exc) from None
            if item['status'] == 'processing':
                self._schedule(item)
            return self._public(item)

    @staticmethod
    def _piece(path, offset, data, compare=False):
        with path.open('rb' if compare else 'r+b', buffering=0) as stream:
            stream.seek(offset)
            if compare:
                if stream.read(len(data)) != data:
                    raise ValueError('이미 받은 파일 조각과 내용이 다릅니다. 처음 선택한 파일로 다시 시도하세요.')
                return
            view = memoryview(data)
            while view:
                written = stream.write(view)
                if not written:
                    raise OSError(errno.ENOSPC, 'write failed')
                view = view[written:]

    def _checkpoint(self, item, offset):
        with self._part(item['id']).open('r+b') as stream:
            stream.flush()
            os.fsync(stream.fileno())
        updated = {**item, 'offset': offset, 'updatedAt': time.time()}
        self._write(updated)
        return updated

    def _rollback(self, ident, original):
        # A lost response/cancellation after the manifest rename is a committed
        # chunk. Keep it; the next status request will return its new offset.
        try:
            committed = self._read(ident)['offset']
        except (OSError, ValueError):
            committed = original
        path = self._part(ident)
        if path.is_file():
            self._truncate(path, committed)

    async def append(self, owner, ident, offset, chunks):
        self._check_open()
        owner, ident = self._owner(owner), self._id(ident)
        if type(offset) is not int or offset < 0:
            raise ValueError('업로드 위치가 올바르지 않습니다.')
        require_upload_storage(self.assets.settings)
        await self._maintenance()
        async with self._lock(ident):
            self._busy.add(ident)
            original = None
            try:
                item = await _io(self._load, ident, owner)
                if item['status'] != 'uploading' or item['offset'] == item['size'] and offset == item['size']:
                    return self._public(item)
                original = item['offset']
                expected = min(CHUNK_BYTES, item['size'] - offset)
                duplicate = offset < original
                if (offset > original or offset % CHUNK_BYTES or expected <= 0
                        or duplicate and offset + expected > original):
                    raise ValueError('업로드 위치가 변경됐습니다. 업로드 상태를 다시 확인하고 이어서 올리세요.')
                if not duplicate and await _io(lambda: shutil.disk_usage(self.root).free) < expected + DISK_HEADROOM:
                    raise OSError(errno.ENOSPC, 'insufficient disk')
                received = 0
                async for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise ValueError('파일 조각 형식이 올바르지 않습니다.')
                    if not chunk:
                        continue
                    if received + len(chunk) > expected:
                        raise ValueError('파일 조각 크기가 올바르지 않습니다. 업로드 상태를 다시 확인하세요.')
                    await _io(self._piece, self._part(ident), offset + received, chunk, duplicate)
                    received += len(chunk)
                if received != expected:
                    raise ValueError('파일 조각 전송이 끝나지 않았습니다. 같은 위치에서 이어서 올리세요.')
                if not duplicate:
                    item = await _io(self._checkpoint, item, offset + received)
                return self._public(item)
            except BaseException as exc:
                if original is not None:
                    try:
                        await _io(self._rollback, ident, original)
                    except OSError:
                        pass  # The durable offset still protects the next retry.
                if isinstance(exc, (FileNotFoundError, PermissionError)):
                    raise
                if isinstance(exc, ConnectionError):
                    raise ValueError('파일 조각 전송이 끊겼습니다. 같은 파일로 이어서 올리세요.') from None
                if isinstance(exc, OSError):
                    raise _storage_error(exc) from None
                raise
            finally:
                self._busy.discard(ident)

    def _schedule(self, item):
        ident = item['id']
        if self._closed or ident in self._tasks:
            return
        stop = self._stops[ident] = threading.Event()
        task = asyncio.create_task(self._finalize(ident, item['owner'], stop), name='upload-finalize-' + ident)
        self._tasks[ident] = task

        def done(completed):
            if self._tasks.get(ident) is completed:
                self._tasks.pop(ident, None)
                self._stops.pop(ident, None)
            if not completed.cancelled():
                completed.exception()  # Never leave an unobserved background error.
        task.add_done_callback(done)

    async def finish(self, owner, ident):
        self._check_open()
        owner, ident = self._owner(owner), self._id(ident)
        require_upload_storage(self.assets.settings)
        await self._maintenance()
        async with self._lock(ident):
            try:
                item = await _io(self._load, ident, owner)
                if item['status'] == 'uploading':
                    if item['offset'] != item['size']:
                        raise ValueError('파일 전송이 아직 끝나지 않았습니다. 남은 부분을 이어서 올리세요.')
                    item = {**item, 'status': 'processing', 'updatedAt': time.time()}
                    await _io(self._write, item)
                if item['status'] == 'processing':
                    self._schedule(item)
                return self._public(item)
            except (FileNotFoundError, PermissionError):
                raise
            except OSError as exc:
                raise _storage_error(exc) from None

    async def _finalize(self, ident, owner, stop):
        item = None
        try:
            async with self._lock(ident):
                item = await _io(self._load, ident, owner)
                if item['status'] != 'processing':
                    return
            # Polling status and duplicate completion requests must stay fast
            # while a large file is being probed/hashed by the worker thread.
            async with self._processing:
                if stop.is_set():
                    raise InterruptedError
                path = self._part(ident)
                if not path.is_file():
                    path = self._target(item)
                inspected = await _io(self.assets.inspect_staged, path, item['size'], stop)
                if stop.is_set():
                    raise InterruptedError
                async with self._lock(ident):
                    if stop.is_set():
                        raise InterruptedError
                    async with self.assets.lock:
                        if stop.is_set():
                            raise InterruptedError
                        result = await _io(self.assets.commit_staged, ident, item['name'], path, item['size'], inspected)
                    item = {**item, 'status': 'complete', 'result': result, 'updatedAt': time.time()}
                    item.pop('error', None)
                    await _io(self._write, item)
        except Exception as exc:
            if item is None:
                return
            async with self._lock(ident):
                result = await _io(self._existing_result, item)
                if result:
                    # Metadata was committed even if updating the session failed.
                    try:
                        await _io(self._write, {**item, 'status': 'complete', 'result': result, 'updatedAt': time.time()})
                    except OSError:
                        pass
                    return
                if isinstance(exc, InterruptedError) and self._closed:
                    return  # Resume durable processing after the next startup.
                message = ('업로드를 취소했습니다.' if isinstance(exc, InterruptedError)
                           else str(_storage_error(exc)) if isinstance(exc, OSError)
                           else str(exc) if isinstance(exc, ValueError)
                           else '파일 검사 중 오류가 발생했습니다. 다시 업로드하세요.')
                failed = {**item, 'status': 'error', 'error': message[:300], 'updatedAt': time.time()}
                try:
                    await _io(self._remove_incomplete, failed)
                    await _io(self._write, failed)
                except OSError:
                    # Leave a recoverable durable record, never a false success.
                    pass

    async def cancel(self, owner, ident):
        self._check_open()
        owner, ident = self._owner(owner), self._id(ident)
        # Ownership is immutable and manifests are atomically replaced. Check
        # before signalling a worker, which may currently own the session lock.
        await _io(self._read, ident, owner)
        task = self._tasks.get(ident)
        if task:
            self._stops[ident].set()
            await asyncio.shield(task)
        async with self._lock(ident):
            try:
                item = await _io(self._load, ident, owner)
                if item['status'] == 'complete':
                    return self._public(item)
                item = {**item, 'status': 'error', 'error': '업로드를 취소했습니다.', 'updatedAt': time.time()}
                await _io(self._remove_incomplete, item)
                await _io(self._write, item)
                return self._public(item)
            except (FileNotFoundError, PermissionError):
                raise
            except OSError as exc:
                raise _storage_error(exc) from None

    async def close(self):
        self._closed = True
        for stop in self._stops.values():
            stop.set()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)
