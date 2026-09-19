"""기존 Firestore 경로를 유지하며, 미설정 시에만 로컬 SQLite 사용.

동기 Firebase/SQLite 작업은 asyncio.to_thread에서 실행하여 음성 heartbeat를 막지 않는다.
한 프로세스/한 replica로 실행한다. 팀 이름은 기존 봇과 동일하게 전체 서버에서 공유된다.
"""
from __future__ import annotations
import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any
from config import Settings
from validation import clean_name

log = logging.getLogger(__name__)


class Store:
    def __init__(self, settings: Settings):
        self.lock = threading.RLock()
        self.db = None
        self.sql = None
        if settings.firebase_key:
            import firebase_admin
            from firebase_admin import credentials, firestore
            # JSON 문자열(기존 형식) 또는 서비스 계정 파일 경로 지원. 오류 시 로컬로 몰래 전환하지 않는다.
            raw = settings.firebase_key
            try:
                value = json.loads(raw) if raw.startswith('{') else str(Path(raw).expanduser())
                cred = credentials.Certificate(value)
                app_name = 'appearance-song-bot'
                try:
                    app = firebase_admin.get_app(app_name)
                except ValueError:
                    app = firebase_admin.initialize_app(cred, name=app_name)
                self.db = firestore.client(app=app)
            except Exception as exc:
                raise RuntimeError('Firebase 인증 설정을 읽지 못했습니다. FIREBASE_SERVICE_ACCOUNT의 JSON/파일 경로를 확인하세요.') from exc
            self.mode = 'firebase'
        else:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            self.sql = sqlite3.connect(settings.data_dir / 'songs.sqlite3', check_same_thread=False)
            self.sql.execute('PRAGMA journal_mode=WAL')
            self.sql.execute('CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, body TEXT NOT NULL)')
            self.sql.commit()
            self.mode = 'local'
            log.warning('Firebase 미설정: 로컬 SQLite 사용. 재배포 시 DATA_DIR의 영구 저장소가 필요합니다.')

    def _read(self, path: str) -> dict | None:
        if self.db is not None:
            d = self.db.document(path).get(timeout=12)
            return d.to_dict() if d.exists else None
        row = self.sql.execute('SELECT body FROM docs WHERE path=?', (path,)).fetchone()
        return json.loads(row[0]) if row else None

    def _write(self, path: str, body: dict, merge: bool = False):
        if self.db is not None:
            self.db.document(path).set(body, merge=merge, timeout=12)
        else:
            if merge:
                body = {**(self._read(path) or {}), **body}
            self.sql.execute('INSERT OR REPLACE INTO docs VALUES (?,?)', (path, json.dumps(body, ensure_ascii=False)))
            self.sql.commit()

    def _list(self, collection: str) -> list[dict]:
        if self.db is not None:
            return [{'id': d.id, **(d.to_dict() or {})} for d in self.db.collection(collection).stream(timeout=12)]
        prefix = collection + '/'
        # LIKE는 팀 이름의 %/_ 문자와 충돌하므로 사용하지 않는다.
        rows = self.sql.execute('SELECT path,body FROM docs WHERE substr(path,1,?)=?', (len(prefix), prefix)).fetchall()
        return [{'id': p[len(prefix):], **json.loads(b)} for p, b in rows if '/' not in p[len(prefix):]]

    def _apply(self, writes: list[tuple[str, dict | None]]):
        if self.db is not None:
            batch = self.db.batch()
            for path, body in writes:
                if body is None:
                    batch.delete(self.db.document(path))
                else:
                    batch.set(self.db.document(path), body)
            batch.commit(timeout=15)
        else:
            with self.sql:
                for path, body in writes:
                    if body is None:
                        self.sql.execute('DELETE FROM docs WHERE path=?', (path,))
                    else:
                        self.sql.execute('INSERT OR REPLACE INTO docs VALUES (?,?)', (path, json.dumps(body, ensure_ascii=False)))

    async def _run(self, func, *args):
        def locked():
            with self.lock:
                return func(*args)
        return await asyncio.to_thread(locked)

    async def teams(self) -> list[str]:
        def work():
            if self.db is not None:
                # 부모 문서 없이 하위 컬렉션만 만들어진 구버전 팀도 찾는다.
                names = {r.id for r in self.db.collection('teams').list_documents(timeout=12)}
            else:
                names = {p[0].split('/')[1] for p in self.sql.execute("SELECT path FROM docs WHERE substr(path,1,6)='teams/'")}
            return sorted(names | {'A팀'})
        return await self._run(work)

    async def create_team(self, team: str):
        team = clean_name(team, '팀 이름')
        await self._run(self._write, f'teams/{team}', {'name': team}, True)

    async def get_team(self, guild_id: int) -> str:
        d = await self._run(self._read, f'guilds/{guild_id}')
        return (d or {}).get('team', 'A팀')

    async def set_team(self, guild_id: int, team: str):
        await self.create_team(team)
        await self._run(self._write, f'guilds/{guild_id}', {'team': team}, True)

    async def songs(self, team: str) -> list[dict]:
        rows = await self._run(self._list, f'teams/{clean_name(team)}/entranceSongs')
        return sorted([{'source': 'youtube', **d, 'name': d['id']} for d in rows], key=lambda d: d['name'])

    async def song(self, team: str, name: str) -> dict | None:
        d = await self._run(self._read, f'teams/{clean_name(team)}/entranceSongs/{clean_name(name)}')
        return {'source': 'youtube', **d, 'name': name} if d is not None else None

    async def save_song(self, team: str, song: dict):
        await self.create_team(team)
        name = clean_name(song['name'])
        body = {k: v for k, v in song.items() if k not in {'name', 'id'}}
        await self._run(self._write, f'teams/{clean_name(team)}/entranceSongs/{name}', body)

    async def delete_song(self, team: str, name: str):
        prefix = f'teams/{clean_name(team)}'
        name = clean_name(name)
        def work():
            writes = [(f'{prefix}/entranceSongs/{name}', None)]
            for row in self._list(f'{prefix}/lineup'):
                if row.get('name') == name:
                    writes.append((f'{prefix}/lineup/{row["id"]}', None))
            self._apply(writes)
        await self._run(work)

    async def lineup(self, team: str) -> dict[str, str]:
        rows = await self._run(self._list, f'teams/{clean_name(team)}/lineup')
        return {str(i): next((d.get('name', '') for d in rows if d['id'] == str(i)), '') for i in range(1, 10)}

    async def save_lineup(self, team: str, values: dict):
        if set(values) - {str(i) for i in range(1, 10)}:
            raise ValueError('타순은 1~9번만 가능합니다.')
        await self.create_team(team)
        writes = []
        for order, name in values.items():
            writes.append((f'teams/{team}/lineup/{order}', {'name': clean_name(name)} if name else None))
        await self._run(self._apply, writes)

    async def state(self, team: str) -> dict:
        team = clean_name(team)
        def work():
            volume = self._read(f'teams/{team}/state/volume') or {}
            game = self._read(f'teams/{team}/state/game') or {}
            return {'volume': max(0.0, min(1.0, float(volume.get('value', .5)))), 'currentOrder': int(game.get('currentOrder', 1))}
        return await self._run(work)

    async def set_volume(self, team: str, value: float):
        if not 0 <= value <= 1:
            raise ValueError('볼륨은 0~100%입니다.')
        await self._run(self._write, f'teams/{clean_name(team)}/state/volume', {'value': value})

    async def set_order(self, team: str, value: int):
        if not 1 <= value <= 9:
            raise ValueError('타순은 1~9번입니다.')
        await self._run(self._write, f'teams/{clean_name(team)}/state/game', {'currentOrder': value}, True)

    async def rename(self, team: str, old: str, new: str):
        team, old, new = clean_name(team), clean_name(old), clean_name(new)
        if old == new:
            raise ValueError('기존 이름과 새 이름이 같습니다.')
        def work():
            root = f'teams/{team}'
            if self._read(f'{root}/entranceSongs/{new}') is not None:
                raise ValueError('새 이름의 등장곡이 이미 있습니다.')
            song = self._read(f'{root}/entranceSongs/{old}')
            lineup = self._list(f'{root}/lineup')
            writes = []
            if song is not None:
                writes += [(f'{root}/entranceSongs/{new}', song), (f'{root}/entranceSongs/{old}', None)]
            for d in lineup:
                if d.get('name') == old:
                    writes.append((f'{root}/lineup/{d["id"]}', {'name': new}))
            if not writes:
                raise ValueError('기존 이름의 등장곡/타순이 없습니다.')
            self._apply(writes)
        await self._run(work)

    async def event(self, team: str, key: str) -> dict | None:
        return await self._run(self._read, f'teams/{clean_name(team)}/events/{clean_name(key)}')

    async def events(self, team: str) -> list[dict]:
        return await self._run(self._list, f'teams/{clean_name(team)}/events')

    async def set_event(self, team: str, key: str, data: dict | None):
        await self.create_team(team)
        await self._run(self._apply, [(f'teams/{clean_name(team)}/events/{clean_name(key)}', data)])

    async def export(self) -> dict:
        result = {'schema': 1, 'note': '오디오 파일은 별도로 DATA_DIR 전체를 백업하세요.', 'teams': {}}
        for name in await self.teams():
            result['teams'][name] = {'songs': await self.songs(name), 'lineup': await self.lineup(name), 'state': await self.state(name), 'events': await self.events(name)}
        return result

    async def close(self):
        if self.sql is not None:
            await self._run(self.sql.close)
