#!/usr/bin/env bash
set -euo pipefail
python -m mujoco_vive_scripts.xr_crosshair_calibration --screen "$@"
