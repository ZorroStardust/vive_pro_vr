#!/usr/bin/env bash
set -Eeuo pipefail

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
unset DISPLAY
unset WAYLAND_DISPLAY

exec > >(tee -a "$LOGDIR/monado.log") 2>&1

echo "[monado] starting at $(date)"
echo "[monado] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "[monado] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"

XRT_LOG=debug \
XRT_COMPOSITOR_LOG=debug \
XRT_COMPOSITOR_PRINT_MODES=1 \
XRT_COMPOSITOR_FORCE_VK_DISPLAY=2 \
XRT_COMPOSITOR_DESIRED_MODE=0 \
monado-service

echo "[monado] exited with $?"
