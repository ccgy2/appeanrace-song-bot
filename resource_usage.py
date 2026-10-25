"""On-demand resource snapshots; no sampler thread, shell commands or billing API.

The snapshot intentionally omits PIDs, command lines, environment variables and
filesystem paths. Container counters describe this process's visible cgroup;
they are not Railway's invoice and may include peers on a shared host cgroup.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import math
import os
from pathlib import Path, PurePosixPath
import re
import threading
import time


def _read(path: Path, limit: int = 65536) -> str:
    try:
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            value = handle.read(limit + 1)
        return value if len(value) <= limit else ''
    except (OSError, ValueError):
        return ''


def _integer(text: str) -> int | None:
    try:
        value = int(text.strip())
        return value if value >= 0 else None
    except (ValueError, TypeError):
        return None


def _fields(text: str) -> dict[str, int]:
    result = {}
    for line in text.splitlines():
        pair = line.split()
        if len(pair) == 2 and (value := _integer(pair[1])) is not None:
            result[pair[0]] = value
    return result


def _unescape(value: str) -> str:
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


class ResourceMeter:
    """Call ``snapshot`` from an executor only when admin diagnostics is opened.

    Reads are cached for five seconds. CPU utilization is a non-blocking delta
    between fresh requests: ``1.0`` means one fully used core over that interval.
    The first request correctly reports ``None`` instead of inventing a rate.
    Injectable filesystem roots and clock also permit offline fixture tests.
    """
    def __init__(self, *, proc_root: Path | str = '/proc',
                 cgroup_root: Path | str = '/sys/fs/cgroup', pid: int | None = None,
                 cache_seconds: float = 5.0, clock=time.monotonic,
                 max_descendants: int = 64, max_threads: int = 128,
                 max_proc_entries: int = 2048):
        self.proc = Path(proc_root)
        self.cgroups = Path(cgroup_root)
        # Some sandbox PID namespaces expose a procfs mounted by the parent.
        # /proc/self still identifies us, whereas /proc/<os.getpid()> may not.
        visible_pid = _integer(_read(self.proc / 'self' / 'stat', 16384).split(' ', 1)[0])
        self.pid = (visible_pid or os.getpid()) if pid is None else pid
        self.cache_seconds = max(0, cache_seconds)
        self.clock = clock
        self.max_descendants = max(1, max_descendants)
        self.max_threads = max(1, max_threads)
        self.max_proc_entries = max(1, max_proc_entries)
        self.lock = threading.Lock()
        self._cached = None
        self._at = None
        self._previous = None
        try:
            self.hz = int(os.sysconf('SC_CLK_TCK'))
            self.page_size = int(os.sysconf('SC_PAGE_SIZE'))
        except (ValueError, OSError, AttributeError):
            self.hz, self.page_size = 100, 4096

    def _process(self, pid: int) -> dict | None:
        raw = _read(self.proc / str(pid) / 'stat', 16384)
        end = raw.rfind(')')
        start = raw.find('(')
        if start < 0 or end < start:
            return None
        values = raw[end + 1:].split()
        try:
            if len(values) < 22:
                return None
            comm = raw[start + 1:end].casefold()
            kind = ('python' if comm.startswith('python') else
                    comm if comm in {'ffmpeg', 'ffprobe', 'deno'} else 'other')
            rss_pages = int(values[21])
            return {'kind': kind, 'rssBytes': max(0, rss_pages) * self.page_size,
                    'cpuSeconds': (int(values[11]) + int(values[12])) / self.hz,
                    'threads': max(0, int(values[17])), 'startTicks': int(values[19])}
        except (ValueError, IndexError, ZeroDivisionError):
            return None

    def _fallback_descendants(self) -> tuple[set[int], bool]:
        """Kernels without task/children expose parent links via stat instead.

        Read only parent relationships for this bounded discovery; retrieve
        resource values afterwards for our descendants only. No process names,
        command lines, environment or unrelated resource values are returned.
        """
        parents, partial, scanned = {}, False, 0
        try:
            for item in self.proc.iterdir():
                if not item.name.isdigit():
                    continue
                if scanned >= self.max_proc_entries:
                    partial = True
                    break
                scanned += 1
                raw = _read(item / 'stat', 16384)
                values = raw[raw.rfind(')') + 1:].split() if ')' in raw else []
                if len(values) >= 2 and (parent := _integer(values[1])) is not None:
                    parents.setdefault(parent, []).append(int(item.name))
        except OSError:
            return set(), True
        visited, queue = {self.pid}, [self.pid]
        while queue:
            for child in parents.get(queue.pop(), []):
                if child in visited:
                    continue
                if len(visited) - 1 >= self.max_descendants:
                    partial = True
                    continue
                visited.add(child)
                queue.append(child)
        return visited - {self.pid}, partial

    def _children(self) -> dict:
        groups = {name: {'kind': name, 'count': 0, 'rssSumBytes': 0, 'cpuSeconds': 0.0}
                  for name in ('python', 'ffmpeg', 'ffprobe', 'deno', 'other')}
        visited, queue = {self.pid}, [self.pid]
        partial, fallback, summarized = False, False, set()
        def append(pid, process):
            group = groups[process['kind']]
            group['count'] += 1
            group['rssSumBytes'] += process['rssBytes']
            group['cpuSeconds'] += process['cpuSeconds']
            summarized.add(pid)
        while queue:
            pid = queue.pop()
            try:
                tasks = (self.proc / str(pid) / 'task').iterdir()
                found = []
                for item in tasks:
                    if not item.name.isdigit():
                        continue
                    if len(found) >= self.max_threads:
                        fallback = True
                        break
                    found.append(item)
            except OSError:
                fallback = True
                continue
            for task in found:
                children_file = task / 'children'
                raw = _read(children_file, 16384)
                if not raw:
                    try:
                        fallback = fallback or not children_file.is_file()
                    except OSError:
                        fallback = True
                for value in raw.split():
                    child = _integer(value)
                    if child is None or child in visited:
                        continue
                    if len(visited) - 1 >= self.max_descendants:
                        partial = True
                        continue
                    visited.add(child)
                    process = self._process(child)
                    if process is None:
                        # Processes may exit between the two proc reads.
                        partial = True
                        continue
                    append(child, process)
                    queue.append(child)
        if fallback:
            children, limited = self._fallback_descendants()
            partial = partial or limited
            for child in children - summarized:
                if len(summarized) >= self.max_descendants:
                    partial = True
                    break
                process = self._process(child)
                if process is None:
                    partial = True
                else:
                    append(child, process)
        rows = [group for group in groups.values() if group['count']]
        return {'count': sum(group['count'] for group in rows), 'byType': rows,
                'rssSumBytes': sum(group['rssSumBytes'] for group in rows),
                'partial': partial,
                'scope': 'own_descendants_only',
                'rssIsSumNotContainerUsage': True}

    def _cgroup_dirs(self) -> dict[str, Path]:
        memberships = []
        for line in _read(self.proc / str(self.pid) / 'cgroup').splitlines():
            parts = line.split(':', 2)
            if len(parts) == 3:
                memberships.append((set(parts[1].split(',')) - {''}, PurePosixPath(parts[2])))
        mounts = []
        for line in _read(self.proc / str(self.pid) / 'mountinfo').splitlines():
            left, sep, right = line.partition(' - ')
            a, b = left.split(), right.split()
            if sep and len(a) >= 5 and len(b) >= 3 and b[0] in {'cgroup', 'cgroup2'}:
                mount = Path(_unescape(a[4]))
                try:
                    mount.relative_to(self.cgroups)
                except ValueError:
                    continue
                mounts.append((b[0], set(b[2].split(',')), PurePosixPath(_unescape(a[3])), mount))
        result = {}
        for controllers, member in memberships:
            if '..' in member.parts:
                continue
            for fs, enabled, root, mount in mounts:
                keys = {'unified'} if fs == 'cgroup2' and not controllers else controllers & enabled
                if not keys:
                    continue
                try:
                    relative = member.relative_to(root)
                except ValueError:
                    if member == PurePosixPath('/'):
                        # A cgroup namespace reports '/' for its own membership
                        # even if mountinfo retains the host's mount root.
                        relative = PurePosixPath('.')
                    else:
                        continue
                path = mount.joinpath(*relative.parts)
                for key in keys:
                    result[key] = path
        # Namespaced containers commonly expose their own cgroup at this root.
        # Only use a fallback when membership explicitly identifies namespace '/'.
        for controllers, member in memberships:
            if member != PurePosixPath('/'):
                continue
            if not controllers:
                result.setdefault('unified', self.cgroups)
            for key in controllers:
                if key in {'memory', 'cpu', 'cpuacct'}:
                    result.setdefault(key, self.cgroups / key)
        return result

    def _container(self) -> tuple[dict, tuple]:
        result = {'available': False, 'version': None, 'memoryCurrentBytes': None,
                  'memoryPeakBytes': None, 'inactiveFileBytes': None,
                  'workingSetEstimateBytes': None, 'memoryLimitBytes': None,
                  'cpuSeconds': None, 'cpuCoresSinceLastSample': None,
                  'cpuLimitCores': None, 'oomKills': None,
                  'scope': 'visible_process_cgroup', 'isRailwayBilling': False}
        locations = self._cgroup_dirs()
        unified = locations.get('unified')
        if unified is not None:
            result['version'] = 2
            stats = _fields(_read(unified / 'memory.stat'))
            result['memoryCurrentBytes'] = _integer(_read(unified / 'memory.current'))
            result['memoryPeakBytes'] = _integer(_read(unified / 'memory.peak'))
            result['memoryLimitBytes'] = _integer(_read(unified / 'memory.max'))
            result['inactiveFileBytes'] = stats.get('inactive_file')
            result['oomKills'] = _fields(_read(unified / 'memory.events')).get('oom_kill')
            micros = _fields(_read(unified / 'cpu.stat')).get('usage_usec')
            result['cpuSeconds'] = micros / 1_000_000 if micros is not None else None
            quota = _read(unified / 'cpu.max').split()
            if len(quota) == 2:
                q, period = _integer(quota[0]), _integer(quota[1])
                if q is not None and period:
                    result['cpuLimitCores'] = q / period
        else:
            memory, cpuacct, cpu = (locations.get(name) for name in ('memory', 'cpuacct', 'cpu'))
            if memory is not None or cpuacct is not None:
                result['version'] = 1
            if memory is not None:
                stats = _fields(_read(memory / 'memory.stat'))
                result['memoryCurrentBytes'] = _integer(_read(memory / 'memory.usage_in_bytes'))
                result['memoryPeakBytes'] = _integer(_read(memory / 'memory.max_usage_in_bytes'))
                limit = _integer(_read(memory / 'memory.limit_in_bytes'))
                result['memoryLimitBytes'] = limit if limit is not None and limit < 1 << 60 else None
                result['inactiveFileBytes'] = stats.get('total_inactive_file', stats.get('inactive_file'))
                result['oomKills'] = _fields(_read(memory / 'memory.oom_control')).get('oom_kill')
            if cpuacct is not None:
                nanos = _integer(_read(cpuacct / 'cpuacct.usage'))
                result['cpuSeconds'] = nanos / 1_000_000_000 if nanos is not None else None
            if cpu is not None:
                q = _integer(_read(cpu / 'cpu.cfs_quota_us'))
                period = _integer(_read(cpu / 'cpu.cfs_period_us'))
                if q is not None and period:
                    result['cpuLimitCores'] = q / period
        current, inactive = result['memoryCurrentBytes'], result['inactiveFileBytes']
        if current is not None and inactive is not None:
            result['workingSetEstimateBytes'] = max(0, current - inactive)
        result['available'] = current is not None or result['cpuSeconds'] is not None
        identity = tuple(sorted((name, str(path)) for name, path in locations.items()))
        return result, identity

    def snapshot(self) -> dict:
        with self.lock:
            now = self.clock()
            if self._cached is not None and self._at is not None and 0 <= now - self._at < self.cache_seconds:
                return deepcopy(self._cached)
            process = self._process(self.pid)
            children = self._children() if process is not None else {
                'count': 0, 'byType': [], 'rssSumBytes': None, 'partial': True,
                'scope': 'own_descendants_only', 'rssIsSumNotContainerUsage': True}
            container, identity = self._container()
            public_process = {'available': process is not None, 'rssBytes': None,
                              'cpuSeconds': None, 'cpuCoresSinceLastSample': None,
                              'uptimeSeconds': None, 'threads': None}
            if process is not None:
                public_process.update({key: process[key] for key in ('rssBytes', 'cpuSeconds', 'threads')})
                try:
                    uptime = float(_read(self.proc / 'uptime').split()[0])
                    if math.isfinite(uptime):
                        public_process['uptimeSeconds'] = round(max(0, uptime - process['startTicks'] / self.hz), 2)
                except (ValueError, IndexError):
                    pass
            interval = None
            if self._previous is not None:
                previous = self._previous
                interval = now - previous['at']
                if interval > 0:
                    for current_row, old_cpu, same_source in [
                        (public_process, previous['processCpu'], process is not None and process['startTicks'] == previous['startTicks']),
                        (container, previous['containerCpu'], identity == previous['identity']),
                    ]:
                        cpu = current_row['cpuSeconds']
                        if same_source and cpu is not None and old_cpu is not None and cpu >= old_cpu:
                            current_row['cpuCoresSinceLastSample'] = round((cpu - old_cpu) / interval, 5)
            self._previous = {'at': now, 'processCpu': public_process['cpuSeconds'],
                              'startTicks': process['startTicks'] if process else None,
                              'containerCpu': container['cpuSeconds'], 'identity': identity}
            value = {'available': public_process['available'] or container['available'],
                     'sampledAtUtc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                     'minimumRefreshSeconds': self.cache_seconds,
                     'cpuSampleIntervalSeconds': round(interval, 3) if interval is not None and interval > 0 else None,
                     'process': public_process, 'children': children, 'container': container,
                     'notes': [
                         '현재 실행 환경의 측정값이며 Railway 청구 금액이 아닙니다.',
                         '하위 프로세스 RSS 합계는 공유 메모리가 중복될 수 있어 컨테이너 메모리와 다릅니다.',
                         '작업 메모리 추정치는 cgroup 메모리에서 비활성 파일 캐시를 뺀 값이며 정확한 과금 기준이 아닙니다.',
                         'CPU 평균은 두 번의 새 측정 사이에서 계산합니다. 첫 측정에는 표시되지 않습니다.',
                         '공유 호스트의 cgroup 수치는 같은 그룹의 다른 작업을 포함할 수 있습니다.',
                     ]}
            self._cached, self._at = value, now
            return deepcopy(value)
