# Unified N150 GitHub Actions Runner Manager

One central Python scheduler for every GitHub Actions worker on the Beelink N150.
This repository replaces the multiple manager.sh loops that live in social-mcp,
but intentionally keeps distinct Docker and KVM executor containers.

**Deployment status: development/PR validation only. This version has NOT
been deployed to N150. Do not run beside old managers.**

## Supported pools

| Pool | Repository | Execution | Lifecycle |
| --- | --- | --- | --- |
| Control | YuriiSokolenko/social-mcp | Persistent Docker, n150-control | Highest priority, durable state |
| Pi agents | YuriiSokolenko/social-mcp | Ephemeral Docker, n150 + pi-agent | Model concurrency gated |
| General CI | YuriiSokolenko/social-mcp | Ephemeral Docker, n150 + general | Docker-aware CI, evidence volume |
| Recursive Coder | YuriiSokolenko/recursive-coder | Ephemeral QEMU/KVM, n150 + recursive-coder-ci | One isolated 2-vCPU/4-GiB guest per job |

Strict config: config/pools.json. Pool credentials must be distinct by
repository (SOCIAL_MCP_GH_TOKEN and RECURSIVE_CODER_GH_TOKEN).
The scheduler owns queue discovery, priority, capacity, fairness, bounded retry,
stale-registration cleanup and durable pending-launch reservations.

The scheduler container has no Docker socket and no /dev/kvm. Those privileges
belong only to separate executors that accept fixed, pool-scoped commands through
Unix sockets. Run-check has its own independently constrained Docker service.
The persistent Control worker is managed but is NOT a second autoscaler.

## Offline developer checks

Requires Python 3.12; optional Docker Compose v2:

    python3 -m compileall -q n150_runner
    python3 -m unittest discover -s tests -p 'test_*.py' -v
    docker compose --env-file .env.example config --quiet
    python3 -m n150_runner plan --config config/pools.json

Offline CI never needs host N150 access, GitHub tokens, a Golden Image,
Docker Engine, KVM hardware or the local inference service.

## Operator preview (NOT deployment)

    cp .env.example .env
    # Populate scoped credentials, protected named volumes and actual host paths.
    docker compose --profile images build worker-pi worker-general worker-control worker-sandbox
    docker compose build scheduler docker-executor kvm-executor run-check-executor

Only after approval and the coordinated stop of old managers should an operator
follow docs/CUTOVER.md. After a planned cutover, status and credentials probes:

    docker compose exec scheduler python3 -m n150_runner status
    docker compose exec scheduler python3 -m n150_runner verify

## Provenance, safety, recovery

- docs/SOURCE_INVENTORY.md — pinned source SHA and original components.
- docs/SECURITY.md — privilege, credential, guest and fork-PR boundaries.
- docs/P0-795.md — candidate KVM admission fault; root cause NOT yet confirmed.
- docs/CUTOVER.md — single downtime-allowed replacement and rollback.

Pinned Golden Image v2: noble-server-cloudimg-amd64-prepared-9fc0ce202ba8.img
SHA-256: 9fc0ce202ba8def25b126323af21d5a5bb0fb8903cb7367f2fc246ae568e1706.
Never overwrite this image, its predecessor, host state or protected volumes.

Epic: https://github.com/YuriiSokolenko/kvm-actions-runner/issues/1
