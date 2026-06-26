#!/usr/bin/env bash
set -euo pipefail

mkdir -p out

python -m mujoco_vive_scripts.save_stereo_frames \
  --model models/stereo_endoscope_test.xml \
  --output-dir out \
  --eye-w 720 \
  --eye-h 800
