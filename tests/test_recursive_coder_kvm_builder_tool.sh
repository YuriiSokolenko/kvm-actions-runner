#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
script="$root/infra/github-runner-autoscaler/install-recursive-coder-kvm-builder-tool.sh"
bash -n "$script"
if grep -E 'docker (restart|stop|rm|prune)|systemctl|sysctl|apparmor|seccomp|sudo[[:space:]]' "$script"; then
  echo 'builder tool script may not touch services or invoke sudo' >&2; exit 1
fi
grep -Fq 'apt-get -s install --no-install-recommends xorriso' "$script"
grep -Fq 'installed_by_this_script' "$script"
grep -Fq 'apt-get remove -y -- xorriso' "$script"
grep -Fq 'no autoremove' "$script"
echo 'builder tool installation script checks passed'
