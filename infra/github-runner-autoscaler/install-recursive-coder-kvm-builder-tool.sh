#!/usr/bin/env bash
# Optional host builder tool, isolated from the production runner manager.
set -euo pipefail
umask 077
state=/var/lib/recursive-coder-kvm-builder
manifest="$state/xorriso-install.json"
mode="${1:---dry-run}"
fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ $# -le 1 ]] || { echo 'Usage: install-recursive-coder-kvm-builder-tool.sh [--dry-run|--install|--verify|--rollback]' >&2; exit 2; }
case "$mode" in --dry-run|--install|--verify|--rollback) ;; *) echo 'invalid mode' >&2; exit 2 ;; esac
if command -v xorriso >/dev/null 2>&1; then installed_before=true; else installed_before=false; fi
case "$mode" in
--dry-run)
  command -v apt-get >/dev/null || fail 'apt-get is required'
  if [[ "$installed_before" = true ]]; then xorriso -version | head -n 1; else apt-get -s install --no-install-recommends xorriso; fi
  echo 'Dry-run only; no package or service changes made.' ;;
--install)
  [[ "$(id -u)" = 0 ]] || fail 'Run --install with owner-approved sudo; this script never invokes sudo'
  command -v apt-get >/dev/null || fail 'apt-get is required'
  install -d -o root -g root -m 0700 "$state"
  exec 9>"$state/install.lock"
  flock -n 9 || fail 'builder-tool installation is already running'
  if [[ -f "$manifest" ]]; then
    [[ "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("schema"))' "$manifest")" = 1 ]] || fail 'unexpected tool state manifest'
  fi
  if [[ "$installed_before" = false ]]; then
    printf '{"schema":1,"installed_by_this_script":true}\n' >"$manifest"
    chown root:root "$manifest"; chmod 0600 "$manifest"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xorriso
  else
    printf '{"schema":1,"installed_by_this_script":false}\n' >"$manifest"
    chown root:root "$manifest"; chmod 0600 "$manifest"
  fi
  xorriso -version | head -n 1
  echo 'xorriso verified; no Docker or runner service was touched.' ;;
--verify)
  command -v xorriso >/dev/null || fail 'xorriso is not installed'
  xorriso -version | head -n 1
  echo 'builder ISO tool verified.' ;;
--rollback)
  [[ "$(id -u)" = 0 ]] || fail 'Run --rollback with owner-approved sudo; this script never invokes sudo'
  [[ -f "$manifest" ]] || { echo 'No builder-tool installation state; nothing to roll back.'; exit 0; }
  owned="$(python3 -c 'import json,sys;print(str(json.load(open(sys.argv[1])).get("installed_by_this_script",False)).lower())' "$manifest")"
  if [[ "$owned" = true ]] && command -v xorriso >/dev/null 2>&1; then apt-get remove -y -- xorriso; fi
  rm -f -- "$manifest"
  echo 'Builder-tool rollback complete; no autoremove or service restart was performed.' ;;
esac
