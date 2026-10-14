# Owner-operated N150 cutover and rollback

**Status: not executed.** Never operate the production Beelink from CI or untrusted public fork PRs. The existing dedicated deployment remains in charge until live acceptance.

## Baseline (read-only)

Record privately the current social-mcp/dev commit, the old KVM manager image and container ID/start time, and container IDs/start times for pi-runner-manager, general-runner-manager and n150-control. Retain protected current Compose/env and rollback instructions. Do not publish credentials or diagnostic files containing secrets.

Verify no active KVM guest and no running or queued-to-start recursive-coder CI job. Verify /dev/kvm, state directory root-owned 0700, existing external Docker volume social-mcp-runner-scheduler, and immutable Golden Image v2 noble-server-cloudimg-amd64-prepared-9fc0ce202ba8.img read-only SHA-256 9fc0ce202ba8def25b126323af21d5a5bb0fb8903cb7367f2fc246ae568e1706. Retain older golden unchanged for rollback.

## Build (no live changes)

Use separate checkout. Copy .env.example to .env, set a repository-scoped RECURSIVE_CODER_GH_ADMIN_TOKEN and chmod 0600 .env. Check docker compose --env-file .env config --quiet and list services (must be exactly kvm-runner-manager). Build **only** new image without starting a second controller.

## Maintenance window

1. Reconfirm no active guests or in-flight CI, snapshot peer manager IDs/start times and protected rollback metadata.
2. Stop **only** the old recursive-coder-kvm-runner-manager. After no QEMU guest remains, remove **only** that stopped dedicated container to free its name; never run global Compose down/prune or remove shared volume.
3. Start **only** the new service from standalone checkout using docker compose --project-name kvm-actions-runner --env-file .env up -d --no-deps kvm-runner-manager.
4. Recheck all three peer IDs/start times unchanged; verify golden, mount, proxy readiness and safe admission logs.
5. Trigger authorized recursive-coder/dev CI. Demonstrate one VM/job and automatic queue pickup under moderate load, VM shutdown/unregister, JUnit artifacts and three green jobs (architecture/unit, full JVM sandbox E2E, contracts), repeatedly without manual restarts. Capture sanitized admission, boot, startup, execution, cleanup times and CPU/memory/disk peaks.

## Fail-safe rollback

On any failed invariant, stop **only** the new KVM service and verify its guest terminates. Restore the retained old dedicated manager from its recorded social-mcp Compose/image/protected env. Never rebuild goldens, remove state/lock volumes, touch peer managers or mutate their workloads. Confirm IDs/start times for peers match pre-cutover baseline.

Only after repeated live success and tested rollback open a separate social-mcp cleanup PR and close [#795](https://github.com/YuriiSokolenko/social-mcp/issues/795) with actual evidence. No N150 host measurements, live deployment, or post-cutover results are claimed by this PR.
