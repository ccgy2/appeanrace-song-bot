"""Resource diagnostics use local proc/cgroup fixtures and one local subprocess."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from resource_usage import ResourceMeter


class MeterFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.cgroup = self.root / 'cgroup'
        self.proc.mkdir()
        self.cgroup.mkdir()
        self.now = 10.0
        self.meter = ResourceMeter(proc_root=self.proc, cgroup_root=self.cgroup,
                                   pid=100, clock=lambda: self.now)
        self.meter.hz, self.meter.page_size = 100, 4096
        self.process(100, 'python3.12', 1, rss=100, cpu=200)
        (self.proc / 'uptime').write_text('30.0 10.0\n')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))

    def process(self, pid, name, parent, *, rss=10, cpu=100, children='', thread=0):
        fields = ['0'] * 22
        fields[0] = 'S'
        fields[1] = str(parent)
        fields[11], fields[12] = str(cpu), '0'
        fields[17] = '1'
        fields[19], fields[21] = '100', str(rss)
        self.write(self.proc / str(pid) / 'stat', f'{pid} ({name}) ' + ' '.join(fields))
        self.write(self.proc / str(pid) / 'task' / str(thread or pid) / 'children', children)

    def unified(self, *, member='/', mountroot='/', current=1000000, inactive=200000, usage=3000000):
        self.write(self.proc / '100/cgroup', f'0::{member}\n')
        point = str(self.cgroup).replace(' ', '\\040')
        self.write(self.proc / '100/mountinfo', f'1 2 0:1 {mountroot} {point} rw - cgroup2 cgroup rw\n')
        relative = Path(member).relative_to(mountroot)
        target = self.cgroup / relative
        self.write(target / 'memory.current', current)
        self.write(target / 'memory.stat', f'inactive_file {inactive}\nfile 400000\n')
        self.write(target / 'memory.max', 'max')
        self.write(target / 'memory.peak', 1100000)
        self.write(target / 'memory.events', 'oom 1\noom_kill 0\n')
        self.write(target / 'cpu.stat', f'usage_usec {usage}\nuser_usec 10\n')
        self.write(target / 'cpu.max', '50000 100000')
        return target

    def test_parent_rss_and_only_own_descendants_are_grouped(self):
        self.process(100, 'python3.12', 1, rss=100, cpu=200, children='101 102')
        self.process(101, 'deno', 100, rss=20, children='103')
        self.process(102, 'ffmpeg', 100, rss=30)
        self.process(103, 'python-worker', 101, rss=40)
        self.process(999, 'SECRET-TOKEN', 1, rss=999999)
        data = self.meter.snapshot()
        self.assertEqual(data['process']['rssBytes'], 100 * 4096)
        self.assertEqual(data['process']['cpuSeconds'], 2)
        self.assertEqual(data['process']['uptimeSeconds'], 29)
        self.assertEqual(data['children']['count'], 3)
        self.assertEqual(data['children']['rssSumBytes'], 90 * 4096)
        self.assertEqual({row['kind'] for row in data['children']['byType']}, {'deno', 'ffmpeg', 'python'})
        text = json.dumps(data)
        for secret in ['SECRET-TOKEN', str(self.root), 'python-worker', 'cmdline', 'environ']:
            self.assertNotIn(secret, text)
        self.assertTrue(data['children']['rssIsSumNotContainerUsage'])

    def test_children_spawned_by_nonleader_threads_are_included(self):
        self.write(self.proc / '100/task/1001/children', '101')
        self.process(101, 'ffprobe', 100)
        self.assertEqual(self.meter.snapshot()['children']['byType'][0]['kind'], 'ffprobe')

    def test_process_name_with_parentheses_does_not_break_stat_parser(self):
        self.process(101, 'weird (secret) )', 100, rss=22)
        self.write(self.proc / '100/task/100/children', '101')
        data = self.meter.snapshot()
        self.assertEqual(data['children']['count'], 1)
        self.assertEqual(data['children']['byType'][0]['kind'], 'other')
        self.assertNotIn('secret', json.dumps(data))

    def test_unified_cgroup_counters_working_set_and_limit(self):
        self.unified()
        data = self.meter.snapshot()['container']
        self.assertEqual(data['version'], 2)
        self.assertEqual(data['memoryCurrentBytes'], 1000000)
        self.assertEqual(data['workingSetEstimateBytes'], 800000)
        self.assertEqual(data['memoryPeakBytes'], 1100000)
        self.assertIsNone(data['memoryLimitBytes'])
        self.assertEqual(data['cpuSeconds'], 3)
        self.assertEqual(data['cpuLimitCores'], .5)
        self.assertFalse(data['isRailwayBilling'])

    def test_nested_membership_and_mount_root_resolve_own_cgroup(self):
        self.unified(member='/container/team/bot', mountroot='/container/team')
        self.assertEqual(self.meter.snapshot()['container']['memoryCurrentBytes'], 1000000)

    def test_cgroup_v1_memory_and_cpuacct_with_combined_cpu_mount(self):
        self.write(self.proc / '100/cgroup', '3:memory:/app\n4:cpu,cpuacct:/app\n')
        self.write(self.proc / '100/mountinfo',
                   f'1 2 0:1 / {self.cgroup}/memory rw - cgroup cgroup rw,memory\n'
                   f'2 2 0:2 / {self.cgroup}/cpu,cpuacct rw - cgroup cgroup rw,cpu,cpuacct\n')
        memory, cpu = self.cgroup / 'memory/app', self.cgroup / 'cpu,cpuacct/app'
        self.write(memory / 'memory.usage_in_bytes', 5000)
        self.write(memory / 'memory.stat', 'total_inactive_file 800\ninactive_file 100\n')
        self.write(memory / 'memory.limit_in_bytes', 9223372036854771712)
        self.write(cpu / 'cpuacct.usage', 2000000000)
        self.write(cpu / 'cpu.cfs_quota_us', 100000)
        self.write(cpu / 'cpu.cfs_period_us', 100000)
        value = self.meter.snapshot()['container']
        self.assertEqual(value['version'], 1)
        self.assertEqual(value['workingSetEstimateBytes'], 4200)
        self.assertIsNone(value['memoryLimitBytes'])
        self.assertEqual(value['cpuSeconds'], 2)
        self.assertEqual(value['cpuLimitCores'], 1)

    def test_cpu_rates_use_nonblocking_deltas_and_cached_calls_do_not_resample(self):
        target = self.unified()
        initial = self.meter.snapshot()
        self.assertIsNone(initial['process']['cpuCoresSinceLastSample'])
        self.assertIsNone(initial['container']['cpuCoresSinceLastSample'])
        self.process(100, 'python', 1, cpu=300)
        self.write(target / 'cpu.stat', 'usage_usec 8000000\n')
        self.now = 12
        cached = self.meter.snapshot()
        self.assertEqual(cached['process']['cpuSeconds'], 2)
        self.now = 15
        value = self.meter.snapshot()
        self.assertEqual(value['cpuSampleIntervalSeconds'], 5)
        self.assertEqual(value['process']['cpuCoresSinceLastSample'], .2)
        self.assertEqual(value['container']['cpuCoresSinceLastSample'], 1)

    def test_counter_reset_never_yields_negative_cpu_rate(self):
        target = self.unified()
        self.meter.snapshot()
        self.now += 5
        self.write(target / 'cpu.stat', 'usage_usec 1')
        self.assertIsNone(self.meter.snapshot()['container']['cpuCoresSinceLastSample'])

    def test_namespaced_v1_combined_mount_uses_mount_root(self):
        self.write(self.proc / '100/cgroup', '4:cpu,cpuacct:/\n')
        self.write(self.proc / '100/mountinfo',
                   f'2 2 0:2 /host/container {self.cgroup}/cpu,cpuacct rw - cgroup cgroup rw,cpu,cpuacct\n')
        self.write(self.cgroup / 'cpu,cpuacct/cpuacct.usage', 3000000000)
        self.assertEqual(self.meter.snapshot()['container']['cpuSeconds'], 3)

    def test_missing_children_files_fall_back_to_parent_links_with_bounded_scan(self):
        (self.proc / '100/task/100/children').unlink()
        self.process(101, 'deno', 100)
        self.process(102, 'ffmpeg', 101)
        self.process(999, 'unrelated', 1)
        data = self.meter.snapshot()['children']
        self.assertEqual(data['count'], 2)
        self.assertNotIn('other', [row['kind'] for row in data['byType']])
        self.now += 5
        self.meter.max_proc_entries = 1
        self.assertTrue(self.meter.snapshot()['children']['partial'])

    def test_absent_or_denied_proc_and_cgroup_are_reported_as_unavailable(self):
        meter = ResourceMeter(proc_root=self.root / 'absent', cgroup_root=self.root / 'missing', pid=100)
        value = meter.snapshot()
        self.assertFalse(value['available'])
        self.assertIsNone(value['process']['rssBytes'])
        self.assertIsNone(value['container']['memoryCurrentBytes'])
        with patch('resource_usage.Path.open', side_effect=PermissionError):
            self.assertFalse(ResourceMeter(proc_root=self.proc, cgroup_root=self.cgroup, pid=100).snapshot()['available'])

    def test_absent_inactive_file_does_not_invent_working_set(self):
        target = self.unified()
        self.write(target / 'memory.stat', 'file 100')
        self.assertIsNone(self.meter.snapshot()['container']['workingSetEstimateBytes'])

    def test_working_set_clamps_to_zero_and_malformed_counters_stay_unknown(self):
        target = self.unified(current=100, inactive=200)
        self.write(target / 'cpu.stat', 'usage_usec not-a-number')
        data = self.meter.snapshot()['container']
        self.assertEqual(data['workingSetEstimateBytes'], 0)
        self.assertIsNone(data['cpuSeconds'])

    def test_descendant_limit_reports_partial_and_handles_cycles_or_disappearing_children(self):
        self.process(100, 'python', 1, children='101 102 103 404 100')
        self.process(101, 'deno', 100, children='100')
        self.process(102, 'ffmpeg', 100)
        self.process(103, 'python', 100)
        self.meter.max_descendants = 2
        data = self.meter.snapshot()['children']
        self.assertTrue(data['partial'])
        self.assertEqual(data['count'], 2)

    def test_returned_dict_does_not_mutate_shared_cache(self):
        first = self.meter.snapshot()
        first['process']['rssBytes'] = -1
        first['notes'].clear()
        second = self.meter.snapshot()
        self.assertGreater(second['process']['rssBytes'], 0)
        self.assertTrue(second['notes'])

    def test_mount_traversal_is_ignored_and_root_fallback_requires_root_membership(self):
        self.write(self.cgroup / 'memory.current', 999999)
        self.write(self.proc / '100/cgroup', '0::/../../secret')
        self.assertFalse(self.meter.snapshot()['container']['available'])


@unittest.skipUnless(sys.platform.startswith('linux') and Path('/proc/self/stat').exists(), 'Linux proc required')
class RealProcessTest(unittest.TestCase):
    def test_local_child_appears_and_disappears_without_sending_commands_or_credentials(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        meter = ResourceMeter(cache_seconds=0)
        try:
            value = meter.snapshot()
            self.assertTrue(value['process']['available'])
            self.assertGreaterEqual(value['children']['count'], 1)
            python_count = sum(row['count'] for row in value['children']['byType'] if row['kind'] == 'python')
            self.assertGreaterEqual(python_count, 1)
            json.dumps(value, allow_nan=False)
        finally:
            child.communicate(timeout=5)
        after = meter.snapshot()
        self.assertLess(after['children']['count'], value['children']['count'])


if __name__ == '__main__':
    unittest.main()
