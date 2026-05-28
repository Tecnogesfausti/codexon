#!/usr/bin/env bash
set -Eeuo pipefail

mkdir -p /share/codex-agent
exec > >(tee -a /share/codex-agent/web-terminal.log) 2>&1

echo "[$(date -Is)] web terminal shell starting"
echo "[$(date -Is)] workspace=${WORKSPACE:-/ha_config}"
cd "${WORKSPACE:-/ha_config}"
exec /bin/bash -l
