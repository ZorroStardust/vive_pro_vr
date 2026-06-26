#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/env.sh"

exec > >(tee -a "$LOGDIR/hello_xr.log") 2>&1

echo "[hello] starting at $(date)"
echo "[hello] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "[hello] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "[hello] socket:"
ls -l "$XDG_RUNTIME_DIR/monado_comp_ipc" || true

export XR_RUNTIME_JSON
export XDG_RUNTIME_DIR
if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
  export WAYLAND_DISPLAY
fi
if [[ -n "${DISPLAY:-}" ]]; then
  export DISPLAY
fi
if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  export DBUS_SESSION_BUS_ADDRESS
fi

echo
echo "[hello] help:"
hello_xr -h || true

echo
echo "[hello] running:"
echo "hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v"

set +e
XR_LOADER_DEBUG=all \
timeout --foreground "${RUN_SECONDS}s" \
hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v
status=$?
set -e

echo "[hello] exit status=$status at $(date)"
exit 0
