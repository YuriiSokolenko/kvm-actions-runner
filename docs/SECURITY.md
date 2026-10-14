# Trust and privilege boundaries

One scheduler holds the existing admission.lock in the preserved shared
scheduler state volume for its whole process lifetime. It computes GitHub
demand, pool fairness/priority, host CPU/memory/disk/model constraints,
Control reservation, retries, retirement and cleanup.

Scheduler: no Docker socket, no /dev/kvm, no Pi config; per-repository GitHub
credentials and Unix sockets for fixed, pool-scoped commands only.

Docker executor: Docker daemon access. Fixed pool images, runner names,
labels, CPU/memory caps, allowed mounts and lifecycle. Launches Control
persistently with its protected state volume; launches Pi and General only
with ephemeral registration tokens. This local socket is never mounted
into untrusted worker containers.

KVM executor: /dev/kvm access and read-only Golden Image + dedicated VM state.
No Docker socket, other repository PAT, host Pi config or shared secrets.
The imported KVM backend enforces only one VM, 2 vCPU, 4 GiB guest RAM,
24 GiB overlay, 1800 s lifetime, restricted nftables egress and proxy.

Run-check executor: narrow HTTP sandbox endpoint with Docker access and
an isolated stage volume; Pi workers have no host Docker socket.

General CI is deliberately Docker-enabled, so it is a high-trust lane:
never schedule a fork PR or unreviewed code on it. Workflow selection is by
exact evaluated labels and named files, not by guessing YAML or broad
substring matches. Fork pull_request and workflow_run payloads are filtered
using head_repository.full_name; malformed GitHub replies fail closed.

Known boundary: a malicious process with host root or host Docker socket
can bypass container-local restrictions. The exact Unix socket ownership
and filesystem ACLs require host review before production acceptance.

Diagnostics expose only reason codes and numeric counts. Never log GitHub
registration tokens, PATs, VM cloud-init payloads, secrets, raw API response
bodies, backend stderr, personal paths or unsanitized logs.
