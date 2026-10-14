# Source inventory and provenance

Original public repository: YuriiSokolenko/social-mcp, dev.
Exact source commit: c7fffb0e2047f15b57f5ef11fb2084cf3166149f.

The original files are imported at their original relative paths under
infra/github-runner-autoscaler/, scripts/pi-common/, .agent-harness.json,
and tests/. No tokens, runtime .env, private configuration, VM images, overlays,
host state, evidence, snapshots, logs or protected credentials were imported.

- kvm-backend.py, kvm-egress-proxy.py, kvm-proxy-bridge:
  one-job QEMU guest, hardened bootstrap, deny-by-default egress, cleanup.
- build-recursive-coder-kvm-golden.sh, prepare-recursive-coder-kvm.sh,
  install-recursive-coder-kvm-builder-tool.sh, recursive-coder-golden-v2-guest.sh:
  preserved Golden Image building tools, not invoked during manager migration.
- worker.Dockerfile, worker-general.Dockerfile, control-runner.Dockerfile:
  pinned worker images for Pi, General CI and persistent Control.
- worker-entrypoint.sh, control-runner-entrypoint.sh:
  unchanged registration, retirement and Control repair/upgrade behavior.
- run-check-executor.mjs, run-check-sandbox.Dockerfile, related Python
  sandbox scripts, Pi adapter and subagent patch scripts:
  original sandbox and hardening logic.
- scripts/pi-common modules and .agent-harness.json:
  required local Docker build dependencies, self-contained in this repo.
- Imported tests:
  test_kvm_backend.py, test_kvm_egress_proxy.py, test_kvm_deploy_policy.py,
  test_build_recursive_coder_kvm_golden.sh,
  test_prepare_recursive_coder_kvm.sh,
  test_recursive_coder_kvm_builder_tool.sh.

Intentionally excluded: manager.sh, manager.Dockerfile,
kvm-manager.Dockerfile, prior multi-manager compose.yaml,
legacy Recursive Coder Docker autoscaler and obsolete multi-manager test harness.
They must not become a second scheduler.

Protected host assets:
- Docker volume social-mcp-runner-scheduler (shared advisory lock/state).
- Docker volumes social-mcp-control-runner-state,
  social-mcp-general-runner-evidence, social-mcp-run-check-stage.
- Host VM state /var/lib/recursive-coder-kvm/state.
- Read-only Golden Image directory /var/lib/recursive-coder-kvm/golden.
- Golden Image v2 and preceding Golden Image retained for rollback.

Do not remove obsolete ownership from social-mcp until the new service is
accepted in production in a separate follow-up cleanup PR.
