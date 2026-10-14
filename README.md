# KVM Actions Runner

Standalone **one-job QEMU/KVM GitHub Actions runner manager** for Beelink N150. Restricted to [YuriiSokolenko/recursive-coder](https://github.com/YuriiSokolenko/recursive-coder).

**Not deployed from this repository.** Requires reviewed PR, green standalone CI, and owner-approved maintenance window.

## Preserved invariants

- Exactly one ephemeral guest/job, 2 vCPUs, 4096 MiB guest RAM, 24 GiB max overlay, 1800-second VM lifetime.
- Docker limits: 2.25 CPU, 5120 MiB memory, 8192 MiB total memory+swap, 1024 PIDs.
- Immutable Golden Image v2: noble-server-cloudimg-amd64-prepared-9fc0ce202ba8.img; SHA-256: 9fc0ce202ba8def25b126323af21d5a5bb0fb8903cb7367f2fc246ae568e1706. Do not rebuild on migration.
- Existing **external** social-mcp-runner-scheduler Docker volume; protected /var/lib/recursive-coder-kvm/state (0700).
- Default-deny guest nftables, allowlisted HTTP(S) egress proxy; no Docker socket, Pi config, host bridge, forwarded ports, or persistent registration token.
- Never modify the Pi, General or Control runner pools.

## Architecture

| Component | Purpose |
| --- | --- |
| manager.py | Dedicated single-VM scheduler, shared flock, retry/backoff, structured denial logs |
| github_api.py | Scoped GitHub API and bounded queues/registration |
| admission.py and host_probe.py | Pure resource admission and host resource sampling |
| kvm-backend.py | Golden validation, VM lifecycle, guest bootstrap, cleanup |
| kvm-egress-proxy.py | Restricted allowlist proxy (unchanged) |
| scripts/ | Opt-in original golden build/prepare helpers |
| docs/ | Source mapping, threat model and reversible cutover |

### Admission incident (social-mcp #795)

The old code enforced 1-minute load + 2 guest vCPUs + 1 reserved CPU <= 4 online CPUs, rejecting new VM starts at N150 load > 1. The replacement allows load <= 3 on N150 **only while** the independent 2.25 CPU cgroup quota, free-memory and disk reserves, shared flock, legacy 11/12 runner reservation, and one-VM cap pass. Invalid readings fail closed and reasons are logged with bounded backoff. This is a source-level reproduction, not yet a live verified production root cause.

Proxy startup now emits a post-bind readiness signal rather than probing the deny-by-default listener using an invalid TCP request.

## Offline verification

Python 3.12+ unit tests:

    python3 -m unittest discover -s tests -p 'test_*.py' -v

Bash parsing:

    for f in scripts/*.sh tests/test_*.sh; do bash -n "$f"; done

Compose config (no startup):

    RECURSIVE_CODER_GH_ADMIN_TOKEN=dummy docker compose config --quiet

See [architecture and provenance](docs/ARCHITECTURE.md) and [owner cutover/rollback](docs/CUTOVER.md). GitHub Actions uses hosted runners, not privileged N150.

Ported from [social-mcp/dev at c7fffb0e](https://github.com/YuriiSokolenko/social-mcp/commit/c7fffb0e2047f15b57f5ef11fb2084cf3166149f) after [PR #794](https://github.com/YuriiSokolenko/social-mcp/pull/794). No root license file found at that source commit; original owner retains attribution. Tracking [epic #1](https://github.com/YuriiSokolenko/kvm-actions-runner/issues/1) and [incident #795](https://github.com/YuriiSokolenko/social-mcp/issues/795).
