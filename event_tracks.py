"""Category-aware event tracks, including legacy situation-only records."""
from validation import song_category, saved_volume_percent
MAX_EVENT_TRACKS = 100

def event_tracks(doc):
    if not doc: return []
    raw = doc.get('tracks')
    if not isinstance(raw, list):
        if doc.get('songName'):
            raw = [{'type': 'song', 'songName': doc['songName'], 'category': doc.get('category', 'situation')}]
        elif doc.get('assetId'):
            raw = [{'type': 'asset', 'assetId': doc['assetId'], 'filename': doc.get('filename'),
                    'volumePercent': saved_volume_percent(doc)}]
        else: return []
    tracks = []
    for item in raw[:MAX_EVENT_TRACKS]:
        if not isinstance(item, dict): continue
        if item.get('type') == 'song' and item.get('songName'):
            # Missing category in old data ALWAYS means situation, never entrance.
            tracks.append({'type': 'song', 'songName': str(item['songName']),
                           'category': song_category(item.get('category', 'situation'))})
        elif item.get('type') == 'asset' and item.get('assetId'):
            tracks.append({'type': 'asset', 'assetId': str(item['assetId']),
                           'filename': str(item.get('filename') or '업로드 오디오'),
                           'volumePercent': saved_volume_percent(item)})
    return tracks
