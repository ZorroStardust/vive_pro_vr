#!/usr/bin/env bash
set -euo pipefail

python -m mujoco_vive_scripts.mujoco_sbs_glfw \
  --model models/stereo_endoscope_test.xml \
  --windowed \
  --width 1440 \
  --height 800 \
  --fps 60
