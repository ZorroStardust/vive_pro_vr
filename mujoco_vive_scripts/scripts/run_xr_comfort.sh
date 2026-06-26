#!/usr/bin/env bash
set -euo pipefail
export XR_RUNTIME_JSON="${XR_RUNTIME_JSON:-/usr/share/openxr/1/openxr_monado.json}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
python -m mujoco_vive_scripts.xr_mujoco_opengl_comfort "$@"
