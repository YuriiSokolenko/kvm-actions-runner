#!/usr/local/bin/python3
"""Fixed sandbox health probe used by the manager gate and runtime preflight."""
import json
import os
import subprocess
import sys


def main():
    require_worktree = len(sys.argv) == 2 and sys.argv[1] == "worktree"
    status = {}
    with open("/proc/self/status", encoding="utf-8") as stream:
        for line in stream:
            if line.startswith(("CapEff:", "NoNewPrivs:")):
                key, value = line.split(":", 1)
                status[key] = value.strip()
    interfaces = sorted(os.listdir("/sys/class/net"))
    socket_absent = not os.path.exists("/var/run/docker.sock")
    workspace_readable = os.path.isdir("/workspace") and os.access("/workspace", os.R_OK | os.X_OK)
    if require_worktree:
        marker = "/workspace/.pi-run-check-preflight"
        workspace_readable = workspace_readable and open(marker, encoding="utf-8").read() == "readable\n"
    forbidden_host_paths = ["/home/runner", "/pi-config-ro", "/host", "/var/run/secrets"]
    host_paths_absent = all(not os.path.exists(item) for item in forbidden_host_paths)
    with open("/proc/self/environ", "rb") as stream:
        environment_names = [item.split(b"=", 1)[0].decode("ascii", errors="ignore") for item in stream.read().split(b"\0") if b"=" in item]
    secret_environment_absent = not any(
        any(marker in name.upper() for marker in ("TOKEN", "SECRET", "API_KEY", "ACCESS_KEY", "GITHUB", "MODEL", "OPENAI", "ANTHROPIC", "SSH_AUTH_SOCK"))
        for name in environment_names
    )
    # Node checks are a supported focused-validation contract, not an optional
    # runner dependency. Verify the pinned binary in the same no-network image.
    try:
        node_probe = subprocess.run(
            ["/usr/local/bin/node", "--version"],
            check=True, capture_output=True, text=True, timeout=3,
        )
        node_available = node_probe.stdout.strip().startswith("v")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        node_available = False
    result = {
        "node_available": node_available,
        "uid": os.getuid(),
        "effective_capabilities": status.get("CapEff"),
        "no_new_privileges": status.get("NoNewPrivs") == "1",
        "network_interfaces": interfaces,
        "docker_socket_absent": socket_absent,
        "worktree_readable": workspace_readable,
        "host_paths_absent": host_paths_absent,
        "secret_environment_absent": secret_environment_absent,
        "checked_host_paths": forbidden_host_paths,
    }
    ok = (
        result["uid"] != 0
        and result["effective_capabilities"] == "0000000000000000"
        and result["no_new_privileges"]
        and interfaces == ["lo"]
        and socket_absent
        and workspace_readable
        and host_paths_absent
        and secret_environment_absent
        and node_available
    )
    result["ok"] = ok
    print(json.dumps(result, separators=(",", ":")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
