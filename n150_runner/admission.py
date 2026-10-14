"""Measured host admission with short, non-secret rejection reasons."""

from __future__ import annotations

import math
import os
import re
import urllib.request
from dataclasses import dataclass

from .config import Host, Pool


class AdmissionError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HostMetrics:
    cpu_count: int
    load_1m: float
    mem_available_mb: int


def host_metrics() -> HostMetrics:
    try:
        cpu_count = os.cpu_count()
        with open("/proc/loadavg", encoding="ascii") as f:
            load = float(f.read().split()[0])
        with open("/proc/meminfo", encoding="ascii") as f:
            memory = next(int(line.split()[1]) // 1024 for line in f if line.startswith("MemAvailable:"))
        if (type(cpu_count) is not int or cpu_count < 1 or not math.isfinite(load)
                or load < 0 or memory < 0):
            raise ValueError("invalid host counters")
    except (OSError, ValueError, IndexError, StopIteration):
        raise AdmissionError("HOST_METRICS_INVALID") from None
    return HostMetrics(cpu_count, load, memory)


def model_capacity(host: Host, active_pi: int, *, opener=urllib.request.urlopen,
                   timeout: int = 5, environ=None) -> int:
    """Cap new Pi sessions by reserved sessions and model runtime load."""
    env = os.environ if environ is None else environ
    url = env.get(host.model_status_url_env, "")
    reserved = min(host.model_max_concurrency, active_pi)
    if not url:
        return max(0, host.model_max_concurrency - reserved)
    if not (url.startswith("http://") or url.startswith("https://")):
        raise AdmissionError("MODEL_URL_INVALID")
    try:
        with opener(url, timeout=timeout) as resp:
            body = resp.read(256 * 1024 + 1)
        if len(body) > 256 * 1024:
            raise ValueError("too large")
        if url.split("?")[0].endswith("/slots"):
            import json
            slots = json.loads(body)
            if not isinstance(slots, list) or not slots or not all(type(s.get("is_processing")) is bool for s in slots if isinstance(s, dict)) or any(not isinstance(s, dict) for s in slots):
                raise ValueError("invalid slot data")
            runtime = min(len(slots), host.model_max_concurrency)
            busy = min(runtime, sum(s["is_processing"] for s in slots))
            return max(0, runtime - max(busy, reserved))
        gauges: dict[str, dict[str, float]] = {}
        pattern = re.compile(r"^(vllm|tensorfold):num_requests_(running|waiting)(?:\{[^}]*\})?\s+([0-9]+(?:\.[0-9]+)?)$")
        for line in body.decode("utf-8").splitlines():
            m = pattern.fullmatch(line.strip())
            if m:
                gauges.setdefault(m[1], {}).setdefault(m[2], 0)
                gauges[m[1]][m[2]] += float(m[3])
        if len(gauges) != 1:
            raise ValueError("unknown model provider")
        provider, values = next(iter(gauges.items()))
        if "waiting" not in values or (provider == "tensorfold" and "running" not in values):
            raise ValueError("partial gauge")
        if values["waiting"] > 0:
            return 0
        return max(0, host.model_max_concurrency - max(int(math.ceil(values.get("running", 0))), reserved))
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        raise AdmissionError("MODEL_STATUS_UNAVAILABLE") from None


def admit(pool: Pool, host: Host, metrics: HostMetrics, *, counts: dict[str, int],
          pools: tuple[Pool, ...], model_available: int,
          kvm_disk_free_mb: int | None = None) -> str:
    """Return ALLOW or a stable denial code. Includes all pools, not just backend."""
    count = counts.get(pool.id, 0)
    if count >= pool.max_runners:
        return "POOL_LIMIT"
    active_ephemeral = sum(counts.get(p.id, 0) for p in pools if p.mode == "ephemeral")
    if pool.mode == "ephemeral" and active_ephemeral >= host.max_ephemeral:
        return "GLOBAL_EPHEMERAL_LIMIT"
    active_cpu = sum(counts.get(p.id, 0) * p.cpus for p in pools)
    if active_cpu + pool.cpus > metrics.cpu_count * host.cpu_overcommit - host.cpu_reserve:
        return "HOST_CPU_BUDGET"
    if metrics.mem_available_mb < host.memory_reserve_mb + pool.memory_mb:
        return "HOST_MEMORY_RESERVE"
    if pool.model_gated and model_available <= 0:
        return "MODEL_CAPACITY"
    if pool.backend == "kvm":
        # Historical P0 #795 rule deliberately retained until real host
        # diagnostics identify the rejecting stage; no speculative relaxation.
        if metrics.load_1m + pool.cpus + host.kvm_cpu_headroom > metrics.cpu_count:
            return "KVM_CPU_HEADROOM"
        if type(kvm_disk_free_mb) is not int or kvm_disk_free_mb < 25 * 1024:
            return "KVM_DISK_RESERVE"
    return "ALLOW"
