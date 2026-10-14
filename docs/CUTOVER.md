# One coordinated N150 cutover and rollback (downtime allowed)

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
