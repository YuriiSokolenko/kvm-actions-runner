#!/bin/sh
set -eu
python3 /usr/local/bin/kvm-backend.py verify
python3 /usr/local/bin/kvm-backend.py recover
python3 /usr/local/bin/kvm-backend.py proxy &
proxy_pid=$!
shutdown() {
  kill "$proxy_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
}
trap shutdown EXIT
trap 'exit 0' TERM INT
python3 -m n150_runner.executor kvm &
server_pid=$!
wait "$server_pid"
