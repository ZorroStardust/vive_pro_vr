#!/usr/bin/env bash
set -e

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json

echo "[INFO] Testing Vulkan..."
hello_xr -G Vulkan

echo "[INFO] Testing OpenGL..."
hello_xr -G OpenGL
