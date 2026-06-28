#!/usr/bin/env bash
set -euo pipefail
python -m mujoco_vive_scripts.xr_mujoco_opengl \
  --screen \
  --model models/stereo_endoscope_test.xml \
  "$@"
