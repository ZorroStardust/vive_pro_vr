#!/usr/bin/env bash
set -e

pkill -9 monado-service monado-cli hello_xr 2>/dev/null || true
rm -f "$XDG_RUNTIME_DIR/monado_comp_ipc"

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json

echo "[INFO] Make sure base stations and controllers are OFF."
echo "[INFO] Starting Monado..."

LIBC_FATAL_STDERR_=1 \
XRT_LOG=info \
PROBER_LOG=info \
VIVE_LOG=info \
XRT_COMPOSITOR_LOG=info \
monado-service
