#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/env.sh"

exec > >(tee -a "$LOGDIR/monado.log") 2>&1

echo "[monado] starting at $(date)"
echo "[monado] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "[monado] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "[monado] WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
echo "[monado] DISPLAY=${DISPLAY:-}"
echo "[monado] HMD_CONNECTOR=$HMD_CONNECTOR"

export XR_RUNTIME_JSON
export XDG_RUNTIME_DIR

# KDE Plasma Wayland 下保留 WAYLAND_DISPLAY/DISPLAY。
# 不要 unset DISPLAY/WAYLAND_DISPLAY，否则会退回 TTY/VkDisplay 那类路线。
if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
  export WAYLAND_DISPLAY
fi
if [[ -n "${DISPLAY:-}" ]]; then
  export DISPLAY
fi
if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  export DBUS_SESSION_BUS_ADDRESS
fi

export XRT_LOG=debug
export XRT_COMPOSITOR_LOG=debug
export XRT_COMPOSITOR_PRINT_MODES=1

# 强制走 Wayland compositor 路线，避免 NVIDIA Xlib direct / TTY direct。
export XRT_COMPOSITOR_FORCE_WAYLAND=1

# 指定 HMD connector。若设为 auto，则让 Monado 自动选择。
if [[ "${HMD_CONNECTOR}" != "auto" && -n "${HMD_CONNECTOR}" ]]; then
  export XRT_COMPOSITOR_WAYLAND_CONNECTOR="$HMD_CONNECTOR"
fi

# 某些 Monado/发行版组合需要这个；你的旧版本曾经会报 wayland_direct/direct_wayland 名称不匹配，
# 所以默认不开。需要时运行主脚本加 --force-wayland-direct。
if [[ "${TRY_FORCE_WAYLAND_DIRECT}" == "1" ]]; then
  export XRT_COMPOSITOR_FORCE_WAYLAND_DIRECT=1
fi

set +e
monado-service
status=$?
set -e

echo "[monado] exited with status=$status at $(date)"
exit "$status"
