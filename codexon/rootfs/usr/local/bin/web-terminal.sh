#!/usr/bin/env bash
set -Eeuo pipefail

mkdir -p /share/codexon
cd "${WORKSPACE:-/ha_config}"
echo "[$(date -Is)] web terminal shell starting"
echo "[$(date -Is)] workspace=${WORKSPACE:-/ha_config}"
exec tmux -u new-session -A -s codexon /bin/bash -l
