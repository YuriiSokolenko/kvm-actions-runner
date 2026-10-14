"""Pure, fail-closed admission policy for the one-VM KVM pool.

The 2.25 CPU Docker quota bounds QEMU and the manager together. Load average
is a pressure signal, not a prediction that the guest will consume 2 cores at
all times. The legacy load + guest_vcpus + reserved <= online_cpus heuristic
denied a 4-core host as soon as load exceeded 1.0.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class HostMetrics:
    online_cpus: int
    load1: float
    mem_available_mb: int
    disk_available_mb: int


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class AdmissionPolicy:
    max_active: int = 1
    host_max_ephemeral: int = 12
    legacy_reserved: int = 11
    guest_cpus: int = 2
    cpu_headroom: int = 1
    host_memory_reserve_mb: int = 3072
    worker_memory_mb: int = 5120
    overlay_max_gib: int = 24
    disk_extra_mb: int = 1024

    def check(self, active: int, metrics: HostMetrics) -> Admission:
        if type(active) is not int or active < 0:
            return Admission(False, "invalid_active_count")
        if active >= self.max_active:
            return Admission(False, "pool_full")
        if active + self.legacy_reserved >= self.host_max_ephemeral:
            return Admission(False, "global_pool_reserved")
        if any(type(v) is not int or v < 0 for v in (
            metrics.online_cpus, metrics.mem_available_mb, metrics.disk_available_mb
        )):
            return Admission(False, "invalid_host_metrics")
        if type(metrics.load1) not in (int, float) or not math.isfinite(metrics.load1) or metrics.load1 < 0:
            return Admission(False, "invalid_host_metrics")
        if metrics.online_cpus < self.guest_cpus + self.cpu_headroom:
            return Admission(False, "cpu_count_insufficient")
        # A *separate*, enforced Docker CPU quota of 2.25 on a 4-core host
        # protects host CPU headroom. Admit moderate load; back off only when
        # measured host pressure reaches online_cpus - reserved CPU.
        if metrics.load1 > metrics.online_cpus - self.cpu_headroom:
            return Admission(False, "cpu_load_high")
        if metrics.mem_available_mb < self.host_memory_reserve_mb + self.worker_memory_mb:
            return Admission(False, "memory_low")
        if metrics.disk_available_mb < self.overlay_max_gib * 1024 + self.disk_extra_mb:
            return Admission(False, "disk_low")
        return Admission(True, "admitted")
