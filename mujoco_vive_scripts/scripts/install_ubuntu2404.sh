#!/usr/bin/env bash
set -euo pipefail

sudo apt update
sudo apt install -y \
  python3.12-venv python3-pip python3-dev build-essential \
  libgl1 libegl1 libglvnd0 libglfw3 libglfw3-dev \
  libx11-6 libxrandr2 libxi6 libxcursor1 libxinerama1 \
  mesa-utils libnvidia-egl-wayland1 \
  libopenxr-loader1 libopenxr-utils libopenxr1-monado monado-service monado-cli xr-hardware

echo "[OK] Ubuntu 24.04 system dependencies installed."
