#!/usr/bin/env bash
set -euo pipefail

MONITOR="${1:-0}"

python -m mujoco_vive_scripts.mujoco_sbs_glfw \
  --model models/stereo_endoscope_test.xml \
  --monitor "$MONITOR" \
  --fullscreen \
  --fps 90
