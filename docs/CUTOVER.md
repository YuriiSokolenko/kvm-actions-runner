# One coordinated N150 cutover and rollback (downtime allowed)

## Current Beelink acceptance status (2026-10-15)

The Beelink offline preflight found the Pi model endpoint
`http://192.168.8.210:3009/metrics` unreachable. **The HP Nano hosting
this endpoint was intentionally powered off**, as confirmed by the owner.
This is an expected external dependency being offline, **not evidence of a
Beelink network, scheduler, or model-server defect**. Do not attempt to
repair networking or relax model-capacity admission based on this result.

The offline Beelink suite passed (52 tests), Docker/Compose and KVM were
available, and the pinned Golden Image checksum matched. The coordinated
cutover and real four-pool acceptance were **not run**. Leave legacy managers
running. When HP Nano is powered on, verify the endpoint and rerun
`sudo bash scripts/beelink-acceptance-sudo.sh --preflight` before considering
`--apply`; no deployment approval is implied by this note.

No no-downtime migration, rolling manager handoff or second authority.
This is a manual operator procedure requiring explicit owner approval.

## 1. Preconditions

1. Save old Compose configuration, image digests, container names, runner IDs,
   pool labels, resource caps and private credentials in an encrypted location.
2. Query GitHub jobs and identify active QEMU VMs by exact name.
   Let active jobs finish or explicitly decide to cancel them.
3. Verify the protected Docker volumes exist, particularly
   social-mcp-runner-scheduler and social-mcp-control-runner-state.
4. Verify read-only Golden Image v2 checksum and keep the preceding image,
   overlays, state, trusted proxy and private KVM configuration.
5. Build and test the new images OFFLINE with pinned dependencies.

## 2. Stop old authorities together

Stop pi-runner-manager, general-runner-manager,
recursive-coder-kvm-runner-manager, any enabled legacy Recursive Coder
Docker manager and the old Control worker once idle.
Remove only the old n150-control CONTAINER to release its name, never the
named Control state volume. Verify no old manager.sh remains running.

NEVER run docker compose down -v, docker system prune --volumes or remove
any Golden Image, rollback data, active overlay or secret.

## 3. Start new isolated backends and SINGLE scheduler

    docker compose --profile images build worker-pi worker-general worker-control worker-sandbox
    docker compose build scheduler docker-executor kvm-executor run-check-executor
    docker compose --env-file .env config --quiet
    docker compose up -d docker-executor kvm-executor run-check-executor
    docker compose run --rm --no-deps scheduler python3 -m n150_runner verify
    docker compose up -d scheduler
    docker compose exec scheduler python3 -m n150_runner status

The external volume social-mcp-runner-scheduler remains the same lock
authority. KVM executor verifies SHA-256 of immutable Golden Image v2
before serving VM launches. Do not rebuild a production Golden Image.

## 4. Acceptance and monitoring

- Pi: trusted Pi workflow starts, sandbox run_check works, disposable
  runner unregisters and its container is cleaned up.
- General: Docker-enabled ci.yml works with evidence volume and clean retirement.
- Control: persistent n150-control responds to CI wake and merge gate while
  other worker pools are saturated; registration cooldown survives restarts.
- Recursive Coder: architecture, JVM integration/sandbox E2E and contract
  jobs all pass and produce JUnit artifacts, never more than one KVM VM.
- Global: host CPU/RAM/disk, model slots, one-admission lock, fair scheduling,
  stale registrations, retry/backoff, fork rejection and redaction.
- Repeat several cycles and check the observed #795 rejection reason and
  eventual recovery. No active job may disappear during restart.

## 5. Rollback (downtime acceptable)

1. Stop the new scheduler so admissions cease. Finish/cancel identified
   in-flight jobs and KVM guests; stop executor services.
2. Stop/remove the new managed n150-control container only when idle, but
   preserve the named Control volume and scheduler lock/state volume.
3. Restart the previous pinned Compose/images/config ONLY after confirming
   the new scheduler is fully stopped. Do not delete Golden Images or state.
4. Confirm old runner IDs/labels, Control wake/merge path and KVM isolation.
   Record the blocker and retained evidence.

Upstream source cleanup happens only after full production acceptance.

## Persistent Control image and configuration upgrades

Control is **persistent**, unlike disposable Pi/General workers. Rebuilding or
retagging the Control image does not upgrade an existing container: Docker
keeps its original image ID and environment. The Docker executor now checks
the image ID, resource constraints, runner labels, restart policy and mounted
Control state before restarting a stopped Control container; mismatches fail
with CONTROL_CONFIG_DRIFT instead of silently starting stale configuration.

During an approved maintenance window: wait for a Control job to finish,
stop the scheduler, inspect and record the running Control image/volumes, stop
and remove **only the n150-control container**, rebuild or pull the target
pinned image, then restart the scheduler. Preserve the named
social-mcp-control-runner-state volume and all repair cooldown records.
Do **not** delete or recreate the Control state volume to resolve drift.
