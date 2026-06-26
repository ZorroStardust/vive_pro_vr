#!/usr/bin/env bash
set -euo pipefail

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json

echo "[INFO] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"

if ! command -v hello_xr >/dev/null 2>&1; then
  echo "[ERROR] hello_xr not found. Install libopenxr-utils."
  exit 1
fi

echo "[INFO] Testing hello_xr Vulkan..."
hello_xr -G Vulkan

echo "[INFO] Testing hello_xr OpenGL..."
hello_xr -G OpenGL
