#!/usr/bin/env bash
set -Eeuo pipefail

LOGDIR="$PWD/logs/monado_vive_crash_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOGDIR"

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:1}"

tmux kill-server 2>/dev/null || true
pkill -9 hello_xr monado-service 2>/dev/null || true
rm -f "$XDG_RUNTIME_DIR/monado_comp_ipc"

{
  echo "== env =="
  env | grep -E 'XDG_SESSION_TYPE|XDG_CURRENT_DESKTOP|WAYLAND_DISPLAY|DISPLAY|XDG_RUNTIME_DIR|XR_RUNTIME_JSON' || true

  echo
  echo "== packages =="
  command -v monado-service || true
  command -v monado-cli || true
  command -v hello_xr || true
  dpkg -S "$(command -v monado-service)" 2>/dev/null || true
  dpkg -l | grep -Ei 'monado|openxr|survive|hidapi|libusb|xr-hardware' || true

  echo
  echo "== drm =="
  for s in /sys/class/drm/card*-*/status; do
    echo "$s: $(cat "$s" 2>/dev/null)"
  done
  for n in /sys/class/drm/card*-*/non_desktop; do
    echo "$n: $(cat "$n" 2>/dev/null)"
  done

  echo
  echo "== usb =="
  lsusb || true
  lsusb -t || true

  echo
  echo "== monado-cli probe =="
  XRT_PRINT_OPTIONS=1 PROBER_LOG=debug VIVE_LOG=debug monado-cli probe 2>&1 || true

} | tee "$LOGDIR/system_probe.txt"

echo
echo "== run monado-service crash reproduction =="
(
  ulimit -c unlimited

  XRT_PRINT_OPTIONS=1 \
  XRT_LOG=debug \
  PROBER_LOG=debug \
  VIVE_LOG=debug \
  XRT_COMPOSITOR_LOG=debug \
  XRT_COMPOSITOR_PRINT_MODES=1 \
  XRT_COMPOSITOR_FORCE_WAYLAND=1 \
  XRT_COMPOSITOR_WAYLAND_CONNECTOR=DP-3 \
  monado-service
) 2>&1 | tee "$LOGDIR/monado_crash_debug.log" || true

echo
echo "== coredump =="
coredumpctl list monado-service 2>/dev/null | tail -20 | tee "$LOGDIR/coredump_list.txt" || true
coredumpctl info monado-service 2>/dev/null | tail -120 | tee "$LOGDIR/coredump_info.txt" || true

echo
echo "LOGDIR=$LOGDIR"
