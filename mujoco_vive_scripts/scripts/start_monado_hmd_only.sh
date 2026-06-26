#!/usr/bin/env bash
set -euo pipefail

pkill -9 monado-service hello_xr monado-cli 2>/dev/null || true
rm -f "$XDG_RUNTIME_DIR/monado_comp_ipc"

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json

echo "[INFO] Keep base stations OFF for this stage."
echo "[INFO] Keep controllers OFF for this stage."
echo "[INFO] Do NOT unset DISPLAY or WAYLAND_DISPLAY under KDE Wayland."
echo "[INFO] Starting monado-service..."

LIBC_FATAL_STDERR_=1 \
XRT_LOG="${XRT_LOG:-info}" \
PROBER_LOG="${PROBER_LOG:-info}" \
VIVE_LOG="${VIVE_LOG:-info}" \
XRT_COMPOSITOR_LOG="${XRT_COMPOSITOR_LOG:-info}" \
monado-service
