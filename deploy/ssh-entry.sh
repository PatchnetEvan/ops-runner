#!/bin/sh
# The SSH forced command for the runner's machine. In ~ops-runner/.ssh/authorized_keys:
#   restrict,command="/opt/ops-runner/deploy/ssh-entry.sh" ssh-ed25519 AAAA... coordinator
# Whatever the client asks to run is ignored: the key can only submit one
# bundle on stdin and read back one JSON answer.
set -eu
set -a
. "${OPS_ENV_FILE:-/etc/ops-runner/env}"
set +a
exec python3 "$(dirname "$0")/../ops_reconcile.py" --bundle
