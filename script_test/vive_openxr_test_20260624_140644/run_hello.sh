#!/usr/bin/env bash
set -Eeuo pipefail

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
unset DISPLAY
unset WAYLAND_DISPLAY

exec > >(tee -a "$LOGDIR/hello_xr.log") 2>&1

echo "[hello] starting at $(date)"
echo "[hello] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "[hello] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "[hello] socket:"
ls -l "$XDG_RUNTIME_DIR/monado_comp_ipc" || true

echo
echo "[hello] help:"
hello_xr -h || true

echo
echo "[hello] running:"
echo "hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v"

XR_LOADER_DEBUG=all \
timeout --foreground "${RUN_SECONDS:-90}s" \
hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v

echo "[hello] exit status: $?"
