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

## Target platform and validation policy

**Beelink N150 running Ubuntu Linux is the sole deployment and acceptance
target for this project.** The runner manager, Docker executors and QEMU/KVM
guests are designed for the Beelink host, not for macOS.

- Do **not** require MacBook/macOS builds, tests or compatibility fixes.
  macOS-only failures of the copied KVM backend (for example `EBADF` or
  exit code `126`) are not release blockers if they do not reproduce on N150.
- GitHub-hosted Linux CI and offline unit tests are useful preliminary checks,
  but **cannot replace integration and acceptance tests on the actual Beelink**.
- Before approval for deployment, validate image builds, Docker socket/Unix
  socket permissions, `/dev/kvm` and QEMU, all four worker pools,
  admissions and recovery, and the observed CPU-admission behavior from #795
  **on Beelink N150**.

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

## GitHub API budget and demand freshness

The scheduler polls at 10-second intervals to monitor runner health and
maintain Control readiness, but GitHub workflow/run/job discovery is cached
per repository and pool selector: a complete, nonempty demand snapshot has
a 30-second TTL, and an empty snapshot has a 60-second TTL. Repeated polls
reuse this verified result; invalid or incomplete replies are never cached.
Runner busy/online status remains live on every poll, so quota accounting
does not rely on stale busy flags.

After the GitHub API signals a rate limit, the repository client cools down
for at least 60 seconds instead of issuing failing calls on every poll.
Capacity admission fails closed during API outages; no stale demand snapshot
is used beyond its TTL. Review API usage before changing poll cadence,
workflow selectors or the number of active workflow runs.

The General workflow_run consumers observed in social-mcp/dev are
pi-reconcile.yml and pi-usage.yml, and both explicitly check out trusted
dev rather than the triggering head. Changes to that trust assumption
require a separate review before production.
