# Architecture and provenance

Upstream source of truth: [social-mcp at c7fffb0e](https://github.com/YuriiSokolenko/social-mcp/tree/c7fffb0e2047f15b57f5ef11fb2084cf3166149f/infra/github-runner-autoscaler).

| Upstream path relative to infra/github-runner-autoscaler | New location |
| --- | --- |
| kvm-backend.py | kvm-backend.py |
| kvm-egress-proxy.py | kvm-egress-proxy.py |
| kvm-proxy-bridge | kvm-proxy-bridge |
| kvm_deploy_policy.py | kvm_deploy_policy.py |
| build-recursive-coder-kvm-golden.sh | scripts/build-recursive-coder-kvm-golden.sh |
| recursive-coder-golden-v2-guest.sh | scripts/recursive-coder-golden-v2-guest.sh |
| prepare-recursive-coder-kvm.sh | scripts/prepare-recursive-coder-kvm.sh |
| install-recursive-coder-kvm-builder-tool.sh | scripts/install-recursive-coder-kvm-builder-tool.sh |
| KVM branch of manager.sh | manager.py, admission.py, host_probe.py, github_api.py |
| tests/test_kvm_backend.py, test_kvm_egress_proxy.py, test_kvm_deploy_policy.py | tests/ |

KVM backend, proxy and tooling were ported without changing guest security policy; the backend additionally emits KVM_PROXY_READY after listener bind. The large shared Pi/LLM/Docker manager is not imported.

The dedicated scheduler holds kvm-manager.lock for lifetime single-controller exclusion, and acquires admission.lock from the existing **external social-mcp-runner-scheduler volume** around its final admission and VM start. The old dedicated manager must be drained and stopped in an owner maintenance window; do not run both controllers.

GitHub API access is constrained to YuriiSokolenko/recursive-coder, ci.yml, and label requirements self-hosted,n150,recursive-coder-ci (optional linux,x64). Fork PRs are excluded. Short-lived runner token reaches the VM backend only through stdin, not command-line arguments. Error responses and subprocess outputs are not logged.

The disposable VM retains restricted QEMU networking, nftables policy drop, allowlisted local HTTP(S) proxy, verified immutable guest disk, no host Docker socket, no LAN bridge, no inbound forwards, and redacted bounded diagnostic storage.

Remaining host acceptance: verify actual cgroup quota, pinned Java 21 / Gradle 9.8.1 / Node 22.21.1 / Python 3.12 / jsonschema 4.25.1 manifest, Bubblewrap sandbox, JUnit, successive three-tier recursive-coder CI, and measured rollback. Hosted unit tests alone do not establish live deployment success. Source cleanup belongs in a later social-mcp PR.
