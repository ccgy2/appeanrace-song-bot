"""Shared capability definitions. Unknown roles fail closed; no account migration needed."""
ROLE_LABELS = {
    'admin': '관리자', 'registrar': '등장곡 등록/삭제자',
    'player': '등장곡 재생자', 'user': '유저', 'pending': '승인 대기',
}
ROLE_CAPABILITIES = {
    'admin': frozenset({'read', 'play', 'music', 'events', 'autoplay', 'teams', 'users', 'backup'}),
    'registrar': frozenset({'read', 'play', 'music', 'events', 'autoplay'}),
    'player': frozenset({'read', 'play', 'autoplay'}),
    'user': frozenset({'read'}),
    'pending': frozenset(),
}
DISCORD_ROLE_NAMES = {
    'registrar': '등장곡 등록/삭제자', 'player': '등장곡 재생자', 'user': '유저',
}
LEGACY_PLAYER_ROLE = '등장곡 재생인'

def allowed(role, capability):
    return capability in ROLE_CAPABILITIES.get(str(role), ())

def required_capability(path, method):
    if path.startswith('/api/users'): return 'users'
    if path in {'/api/export', '/api/backup'}: return 'backup'
    if path in {'/api/team', '/api/lineup'}: return 'teams'
    if path in {'/api/songs', '/api/upload'}: return 'music'
    if path == '/api/events': return 'events'
    if path == '/api/playback-settings' and method not in {'GET', 'HEAD'}: return 'autoplay'
    if path == '/api/control': return 'play'
    return 'read'

def discord_role(member, owner_id=0):
    if member is None: return 'user'
    if (member.id == owner_id or getattr(getattr(member, 'guild_permissions', None), 'administrator', False)):
        return 'admin'
    names = {r.name for r in getattr(member, 'roles', ())}
    if DISCORD_ROLE_NAMES['registrar'] in names: return 'registrar'
    if names & {DISCORD_ROLE_NAMES['player'], LEGACY_PLAYER_ROLE}: return 'player'
    return 'user'
