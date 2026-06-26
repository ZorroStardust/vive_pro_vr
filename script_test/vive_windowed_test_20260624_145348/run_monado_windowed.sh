#!/usr/bin/env bash
set -Eeuo pipefail

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

exec > >(tee -a "$LOGDIR/monado.log") 2>&1

echo "[monado windowed] starting at $(date)"
echo "[monado windowed] WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
echo "[monado windowed] DISPLAY=${DISPLAY:-}"

XRT_LOG=info \
XRT_COMPOSITOR_LOG=info \
XRT_COMPOSITOR_FORCE_WAYLAND=1 \
monado-service
