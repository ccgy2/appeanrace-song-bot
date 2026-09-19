"""Report the actual upload filesystem; never pretend a Docker directory is a volume."""
from __future__ import annotations
import os
from pathlib import Path


def is_mount_path(path: Path) -> bool:
    path = path.resolve()
    if os.path.ismount(path):
        return True
    # os.path.ismount may miss same-filesystem bind mounts on Linux.
    try:
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields = line.split()
            if len(fields) > 4:
                name = fields[4]
                for before, after in [(r'\040', ' '), (r'\011', '\t'), (r'\012', '\n'), (r'\134', '\\')]:
                    name = name.replace(before, after)
                if Path(name).resolve() == path:
                    return True
    except OSError:
        pass
    return False


def storage_status(settings) -> dict:
    directory = settings.data_dir.resolve()
    volume = settings.volume_path.resolve() if settings.volume_path else None
    within = bool(volume and directory.is_relative_to(volume))
    mounted = bool(volume and is_mount_path(volume))
    durable = bool(within and mounted)
    if settings.on_railway:
        if not volume:
            message = ('오디오 영구 저장소가 연결되지 않았습니다. Railway 봇 서비스에 Volume을 연결하고 '
                       'Mount Path와 DATA_DIR를 같은 경로(새로 설정할 때는 /data)로 맞춰주세요. 설정 전에는 파일 업로드가 차단됩니다.')
        elif not within:
            message = (f'오디오 저장 경로({directory})가 Volume({volume}) 밖에 있습니다. '
                       f'기존 파일을 먼저 백업한 뒤 Railway DATA_DIR를 {volume}로 맞추세요. 자동으로 파일을 옮기거나 삭제하지 않습니다.')
        elif not mounted:
            message = ('Railway Volume 경로는 설정됐지만 실제 마운트를 확인하지 못했습니다. '
                       '봇 서비스와 볼륨 연결 및 실행 로그를 확인하세요. 파일 업로드는 차단됩니다.')
        else:
            message = '오디오를 Railway 영구 볼륨에 저장합니다. 볼륨을 유지하면 재시작·재배포 후에도 파일을 사용할 수 있습니다.'
        label = '영구 저장 연결됨' if durable else '영구 저장 설정 필요'
        status = 'persistent' if durable else 'unsafe'
    else:
        message = ('고정된 로컬 폴더에 저장합니다. 같은 폴더를 유지해야 파일이 남습니다. '
                   'Docker·다른 호스팅은 이 경로에 영구 볼륨을 직접 연결해야 합니다.')
        label, status = '로컬 저장 · 폴더 보존 필요', 'local'
    return {'status': status, 'persistent': durable if settings.on_railway else None,
            'uploadsAllowed': not settings.on_railway or durable, 'label': label, 'message': message,
            'dataDir': str(directory), 'uploadDir': str(directory / 'uploads'),
            'volumeMount': str(volume) if volume else None}


def require_upload_storage(settings):
    state = storage_status(settings)
    if not state['uploadsAllowed']:
        raise ValueError(state['message'])
