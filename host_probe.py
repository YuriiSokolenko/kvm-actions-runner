"""Host resources; read host-wide procfs and the mounted VM state filesystem."""
from __future__ import annotations

import os
from pathlib import Path

from admission import HostMetrics


class HostProbe:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir

    def sample(self) -> HostMetrics:
        cpus = os.cpu_count()
        if cpus is None:
            raise ValueError("CPU count unavailable")
        load1 = os.getloadavg()[0]
        mem_kib = None
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                mem_kib = int(line.split()[1])
                break
        if mem_kib is None:
            raise ValueError("MemAvailable unavailable")
        disk = os.statvfs(self.state_dir)
        return HostMetrics(
            online_cpus=cpus,
            load1=load1,
            mem_available_mb=mem_kib // 1024,
            disk_available_mb=(disk.f_bavail * disk.f_frsize) // (1024 * 1024),
        )
