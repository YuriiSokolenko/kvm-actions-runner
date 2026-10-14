"""Strict immutable configuration for one N150 runner control plane."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ID = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LABEL = re.compile(r"^[A-Za-z0-9_.-]+$")
PREFIX = re.compile(r"^[A-Za-z0-9-]{1,64}$")
ENV = re.compile(r"^[A-Z][A-Z0-9_]*$")
IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./:@_-]{0,253}$")
EVENTS = frozenset({"push", "pull_request", "workflow_dispatch", "schedule", "repository_dispatch", "workflow_run"})


def _only(obj: object, expected: set[str], required: set[str], where: str) -> dict:
    if not isinstance(obj, dict) or set(obj) - expected or required - set(obj):
        raise ValueError(f"{where}: unknown or missing configuration keys")
    return obj


def _integer(value: object, where: str, minimum: int = 0, maximum: int = 999999) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{where}: invalid integer")
    return value


def _number(value: object, where: str, minimum: float, maximum: float) -> float:
    if type(value) not in (float, int) or not minimum <= value <= maximum:
        raise ValueError(f"{where}: invalid number")
    return float(value)


def _text(value: object, pattern: re.Pattern, where: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{where}: invalid value")
    return value


def _names(value: object, where: str, pattern: re.Pattern = LABEL) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not pattern.fullmatch(v) for v in value):
        raise ValueError(f"{where}: expected nonempty name array")
    if len(set(v.lower() for v in value)) != len(value):
        raise ValueError(f"{where}: duplicate names")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class Host:
    max_ephemeral: int
    memory_reserve_mb: int
    cpu_reserve: float
    cpu_overcommit: float
    kvm_cpu_headroom: float
    model_max_concurrency: int
    model_status_url_env: str


@dataclass(frozen=True, slots=True)
class Pool:
    id: str
    repository: str
    token_env: str
    backend: str
    mode: str
    labels: tuple[str, ...]
    workflows: tuple[str, ...]
    events: tuple[str, ...]
    runner_prefix: str
    runner_name: str
    runner_image: str
    max_runners: int
    priority: int
    weight: int
    cpus: float
    memory_mb: int
    network: str
    mount_pi_config: bool
    mount_docker_socket: bool
    model_gated: bool
    idle_polls: int


@dataclass(frozen=True, slots=True)
class Configuration:
    poll_seconds: int
    state_dir: Path
    socket_dir: Path
    host: Host
    pools: tuple[Pool, ...]

    def pools_for_backend(self, backend: str) -> tuple[Pool, ...]:
        return tuple(p for p in self.pools if p.backend == backend)


def parse_config(raw: object) -> Configuration:
    outer = _only(raw, {"poll_seconds", "state_dir", "socket_dir", "host", "pools"},
                  {"poll_seconds", "state_dir", "socket_dir", "host", "pools"}, "root")
    poll = _integer(outer["poll_seconds"], "poll_seconds", 2, 300)
    state_dir = Path(outer["state_dir"])
    socket_dir = Path(outer["socket_dir"])
    if any(not isinstance(s, str) for s in (outer["state_dir"], outer["socket_dir"])):
        raise ValueError("paths must be strings")
    if not state_dir.is_absolute() or not socket_dir.is_absolute() or state_dir == socket_dir:
        raise ValueError("state_dir and socket_dir must be distinct absolute paths")
    host_raw = _only(outer["host"], {"max_ephemeral", "memory_reserve_mb", "cpu_reserve",
                                      "cpu_overcommit", "kvm_cpu_headroom", "model_max_concurrency",
                                      "model_status_url_env"},
                     {"max_ephemeral", "memory_reserve_mb", "cpu_reserve",
                      "cpu_overcommit", "kvm_cpu_headroom", "model_max_concurrency",
                      "model_status_url_env"}, "host")
    host = Host(
        max_ephemeral=_integer(host_raw["max_ephemeral"], "max_ephemeral", 1, 32),
        memory_reserve_mb=_integer(host_raw["memory_reserve_mb"], "memory_reserve_mb", 512, 65536),
        cpu_reserve=_number(host_raw["cpu_reserve"], "cpu_reserve", 0.25, 8),
        cpu_overcommit=_number(host_raw["cpu_overcommit"], "cpu_overcommit", 1, 3),
        kvm_cpu_headroom=_number(host_raw["kvm_cpu_headroom"], "kvm_cpu_headroom", 0.5, 8),
        model_max_concurrency=_integer(host_raw["model_max_concurrency"], "model_max_concurrency", 1, 64),
        model_status_url_env=_text(host_raw["model_status_url_env"], ENV, "model_status_url_env"),
    )
    allowed = {"id", "repository", "token_env", "backend", "mode", "labels", "workflows", "events",
               "runner_prefix", "runner_name", "runner_image", "max_runners", "priority", "weight",
               "cpus", "memory_mb", "network", "mount_pi_config", "mount_docker_socket",
               "model_gated", "idle_polls"}
    if not isinstance(outer["pools"], list) or len(outer["pools"]) < 4:
        raise ValueError("at least four pools (including Control and KVM) are required")
    pools: list[Pool] = []
    for obj in outer["pools"]:
        p = _only(obj, allowed, allowed, "pool")
        backend = p["backend"]
        mode = p["mode"]
        if backend not in ("docker", "kvm") or mode not in ("ephemeral", "persistent"):
            raise ValueError("unknown backend or lifecycle policy")
        for name in ("mount_pi_config", "mount_docker_socket", "model_gated"):
            if type(p[name]) is not bool:
                raise ValueError(f"pool {name} must be boolean")
        labels = _names(p["labels"], "labels")
        if any(x.lower() in ("self-hosted", "linux", "x64") for x in labels):
            raise ValueError("runner labels must omit built-in labels")
        workflow_names = _names(p["workflows"], "workflows", re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")) if mode == "ephemeral" else ()
        if mode == "persistent" and p["workflows"] != []:
            raise ValueError("persistent Control must not be workflow-autoscaled")
        events = _names(p["events"], "events") if mode == "ephemeral" else ()
        if any(e not in EVENTS for e in events) or (mode == "persistent" and p["events"] != []):
            raise ValueError("unsupported workflow events")
        pool = Pool(
            id=_text(p["id"], ID, "pool id"),
            repository=_text(p["repository"], REPO, "repository"),
            token_env=_text(p["token_env"], ENV, "token_env"),
            backend=backend, mode=mode, labels=labels, workflows=workflow_names,
            events=events,
            runner_prefix=_text(p["runner_prefix"], PREFIX, "runner_prefix") if mode == "ephemeral" else "",
            runner_name=_text(p["runner_name"], PREFIX, "runner_name") if mode == "persistent" else "",
            runner_image=_text(p["runner_image"], IMAGE, "runner_image") if backend == "docker" else "",
            max_runners=_integer(p["max_runners"], "max_runners", 1, 16),
            priority=_integer(p["priority"], "priority", 0, 1000),
            weight=_integer(p["weight"], "weight", 1, 100),
            cpus=_number(p["cpus"], "cpus", 0.1, 8),
            memory_mb=_integer(p["memory_mb"], "memory_mb", 256, 16384),
            network=p["network"],
            mount_pi_config=p["mount_pi_config"],
            mount_docker_socket=p["mount_docker_socket"],
            model_gated=p["model_gated"],
            idle_polls=_integer(p["idle_polls"], "idle_polls", 2, 30),
        )
        if mode == "persistent" and (pool.max_runners != 1 or backend != "docker"):
            raise ValueError("persistent Control requires one Docker worker")
        if backend == "kvm" and (mode != "ephemeral" or pool.max_runners != 1
                                 or pool.cpus != 2 or pool.memory_mb != 4096
                                 or pool.runner_image != "" or pool.network != "isolated"):
            raise ValueError("KVM must preserve its verified one-VM/two-vCPU/four-GiB policy")
        if backend != "docker" and (pool.mount_pi_config or pool.mount_docker_socket or pool.model_gated):
            raise ValueError("KVM may not import Docker, Pi or model credentials")
        if pool.mount_pi_config and (backend != "docker" or pool.mount_docker_socket or not pool.model_gated):
            raise ValueError("Pi isolation requires Docker without docker.sock and model admission")
        if pool.mount_docker_socket and (backend != "docker" or pool.model_gated):
            raise ValueError("Docker-enabled CI must remain in its own non-model-gated pool")
        if pool.network not in ("host", "bridge", "isolated"):
            raise ValueError("network must be host, bridge or isolated")
        if pool.network == "isolated" and backend != "kvm":
            raise ValueError("isolated network reserved for KVM backend")
        if p["runner_name"] != (pool.runner_name or "") or p["runner_prefix"] != (pool.runner_prefix or ""):
            raise ValueError("runner identity must agree with lifecycle")
        if p["runner_image"] != pool.runner_image:
            raise ValueError("KVM must not use a Docker runner image")
        pools.append(pool)
    if len(set(p.id for p in pools)) != len(pools):
        raise ValueError("pool ids must be unique")
    identities = [p.runner_prefix or p.runner_name for p in pools]
    if len(set(identities)) != len(identities):
        raise ValueError("runner prefixes/names must be unique")
    for i, a in enumerate(pools):
        for b in pools[i+1:]:
            if a.repository == b.repository and set(a.labels) == set(b.labels):
                raise ValueError("same-repository pools may not share runner labels")
            if a.repository == b.repository and a.mode == "ephemeral" and b.mode == "ephemeral" and (
                a.runner_prefix.startswith(b.runner_prefix + "-") or b.runner_prefix.startswith(a.runner_prefix + "-")
            ):
                raise ValueError("overlapping runner prefixes")
    if len([p for p in pools if p.mode == "persistent"]) != 1:
        raise ValueError("exactly one persistent Control pool is required")
    if len([p for p in pools if p.backend == "kvm"]) != 1:
        raise ValueError("exactly one isolated KVM pool is required")
    return Configuration(poll, state_dir, socket_dir, host, tuple(pools))


def load_config(path: str | Path | None = None) -> Configuration:
    selected = Path(path or os.environ.get("MANAGER_CONFIG", "/etc/n150/pools.json"))
    return parse_config(json.loads(selected.read_text(encoding="utf-8")))


def verify_credentials(config: Configuration, env: dict[str, str] | None = None) -> None:
    values = os.environ if env is None else env
    for repo in {p.repository for p in config.pools}:
        refs = {p.token_env for p in config.pools if p.repository == repo}
        if len(refs) != 1:
            raise ValueError(f"repository {repo}: pools must share exactly one scoped token variable")
        if not all(values.get(ref, "") for ref in refs):
            raise ValueError(f"repository {repo}: missing required credential environment variable")
