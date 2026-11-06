#!/usr/bin/env bash
#
# Launch the dbscope MCP server in Docker over stdio.
#
# The repository directory is never mounted. Only the directory that contains
# config.toml (read-only) and the audit directory (writable) are mounted.
# --network host lets the container inherit VPN routes available on the host.
#
set -euo pipefail

CONFIG="${DBSCOPE_CONFIG:-$HOME/.config/dbscope/config.toml}"
IMAGE="${DBSCOPE_IMAGE:-dbscope:local}"

if [[ ! -f "$CONFIG" ]]; then
  echo "dbscope: config file not found; check DBSCOPE_CONFIG." >&2
  exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "dbscope: image $IMAGE not found; run: docker build -t $IMAGE ." >&2
  exit 1
fi

CONFIG_DIR="$(cd "$(dirname "$CONFIG")" && pwd)"
CONFIG_NAME="$(basename "$CONFIG")"
AUDIT_DIR="${DBSCOPE_AUDIT_DIR:-$HOME/.local/state/dbscope}"
NETWORK="${DBSCOPE_NETWORK:-host}"

mkdir -p "$AUDIT_DIR"
chmod 700 "$AUDIT_DIR"

exec docker run -i --rm \
  --network "$NETWORK" \
  --user "$(id -u):$(id -g)" \
  -e "DBSCOPE_CONFIG=/config/$CONFIG_NAME" \
  -e "DBSCOPE_AUDIT_LOG=/audit/audit.jsonl" \
  -v "$CONFIG_DIR:/config:ro" \
  -v "$AUDIT_DIR:/audit" \
  "$IMAGE"
