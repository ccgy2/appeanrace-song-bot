"""Optional one-time Firestore -> Railway SQLite migration.

Runtime storage remains SQLite. This module is only used when
MIGRATE_FIRESTORE_ONCE=true and FIREBASE_SERVICE_ACCOUNT is present.
Existing SQLite records win; Firestore never overwrites them.
"""
from __future__ import annotations
import json
import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)
KNOWN_TEAM_COLLECTIONS = ('entranceSongs','cheerSongs','situationSongs','lineup','state','events')


def _cred_value(raw: str):
    raw = raw.strip()
    return json.loads(raw) if raw.startswith('{') else str(Path(raw).expanduser())


def _put_missing(conn: sqlite3.Connection, path: str, body: dict) -> bool:
    cur = conn.execute('INSERT OR IGNORE INTO docs(path,body) VALUES (?,?)',
                       (path, json.dumps(body or {}, ensure_ascii=False)))
    return cur.rowcount > 0


def migrate_firestore_once(data_dir: Path) -> dict:
    if os.getenv('MIGRATE_FIRESTORE_ONCE','').strip().lower() not in {'1','true','yes','on'}:
        return {'requested': False, 'copied': 0}
    raw = os.getenv('FIREBASE_SERVICE_ACCOUNT','').strip()
    if not raw:
        log.warning('MIGRATE_FIRESTORE_ONCE=true이지만 FIREBASE_SERVICE_ACCOUNT가 없어 마이그레이션을 건너뜁니다.')
        return {'requested': True, 'copied': 0, 'error': 'missing credentials'}

    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / 'songs.sqlite3'
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, body TEXT NOT NULL)')
    marker = conn.execute("SELECT 1 FROM docs WHERE path='migration/firestore-v1'").fetchone()
    if marker:
        conn.close()
        log.info('Firestore -> SQLite 마이그레이션은 이미 완료되어 건너뜁니다.')
        return {'requested': True, 'copied': 0, 'alreadyDone': True}

    import firebase_admin
    from firebase_admin import credentials, firestore
    app_name = 'appearance-legacy-migration'
    try:
        app = firebase_admin.get_app(app_name)
    except ValueError:
        app = firebase_admin.initialize_app(credentials.Certificate(_cred_value(raw)), name=app_name)
    db = firestore.client(app=app)
    copied = 0
    try:
        # Team parent docs plus their known subcollections.
        team_refs = list(db.collection('teams').list_documents(timeout=20))
        team_names = {r.id for r in team_refs}
        for ref in team_refs:
            snap = ref.get(timeout=20)
            if snap.exists:
                copied += _put_missing(conn, f'teams/{ref.id}', snap.to_dict() or {})
        for team in sorted(team_names | {'A팀','청팀','백팀'}):
            for col in KNOWN_TEAM_COLLECTIONS:
                for snap in db.collection(f'teams/{team}/{col}').stream(timeout=20):
                    copied += _put_missing(conn, f'teams/{team}/{col}/{snap.id}', snap.to_dict() or {})
        # Guild configuration and web accounts.
        for col in ('guilds','webUsers'):
            for snap in db.collection(col).stream(timeout=20):
                copied += _put_missing(conn, f'{col}/{snap.id}', snap.to_dict() or {})
        _put_missing(conn, 'migration/firestore-v1', {'copied': copied, 'done': True})
        conn.commit()
        log.warning('Firestore 기존 데이터 %s개를 Railway SQLite로 비파괴 복사했습니다.', copied)
        return {'requested': True, 'copied': copied, 'done': True}
    except Exception as exc:
        conn.rollback()
        log.warning('Firestore 기존 데이터 복구를 아직 수행하지 못했습니다: %s', exc)
        return {'requested': True, 'copied': copied, 'error': str(exc)}
    finally:
        conn.close()
