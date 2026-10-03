from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from awbotnest.resources import ResourceSampler


class ResourceTests(TestCase):
    def sample(self, values):
        def read(path, **kwargs):
            name = str(path).replace("\\", "/")
            if name not in values:
                raise OSError("not mounted")
            return str(values[name])

        with patch("awbotnest.resources.Path.read_text", read), \
             patch("awbotnest.resources.platform.system", return_value="Linux"), \
             patch("awbotnest.resources.psutil.virtual_memory", return_value=SimpleNamespace(
                 used=8 * 1024**3, total=16 * 1024**3,
             )), patch("awbotnest.resources.psutil.cpu_percent", return_value=0):
            return ResourceSampler().snapshot()

    def test_unlimited_container_uses_its_usage_not_host_usage(self):
        data = self.sample({"/sys/fs/cgroup/memory.current": 256 * 1024**2,
                            "/sys/fs/cgroup/memory.max": "max"})
        self.assertEqual(data["memory_used_mb"], 256)
        self.assertEqual(data["memory_limit_mb"], 16 * 1024)

    def test_container_limit_equal_or_larger_than_host_keeps_container_usage(self):
        for limit in (16 * 1024**3, 32 * 1024**3):
            data = self.sample({"/sys/fs/cgroup/memory.current": 256 * 1024**2,
                                "/sys/fs/cgroup/memory.max": limit})
            self.assertEqual(data["memory_used_mb"], 256)
            self.assertEqual(data["memory_limit_mb"], 16 * 1024)

    def test_zero_usage_is_valid(self):
        data = self.sample({"/sys/fs/cgroup/memory.current": 0,
                            "/sys/fs/cgroup/memory.max": 1024**3})
        self.assertEqual(data["memory_used_mb"], 0)
        self.assertEqual(data["memory_limit_mb"], 1024)

    def test_unlimited_v1_container_and_missing_cgroup(self):
        data = self.sample({"/sys/fs/cgroup/memory/memory.usage_in_bytes": 256 * 1024**2,
                            "/sys/fs/cgroup/memory/memory.limit_in_bytes": 2**63 - 1})
        self.assertEqual(data["memory_used_mb"], 256)
        self.assertEqual(self.sample({})["memory_used_mb"], 8 * 1024)
