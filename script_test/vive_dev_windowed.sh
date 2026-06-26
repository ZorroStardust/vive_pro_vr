#!/usr/bin/env bash
set -Eeuo pipefail

SESSION="vive_dev_windowed"
LOGDIR="$HOME/vive_windowed_test_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOGDIR"
ln -sfn "$LOGDIR" "$HOME/vive_windowed_latest"

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# 注意：桌面窗口模式不要 unset DISPLAY / WAYLAND_DISPLAY
# 需要从 GNOME/KDE 桌面终端中运行
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
  echo "ERROR: no WAYLAND_DISPLAY or DISPLAY. Run this from desktop terminal, not TTY."
  exit 1
fi

APP_CMD=("$@")
if [ "$#" -eq 0 ]; then
  APP_CMD=(hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v)
fi

exec > >(tee -a "$LOGDIR/driver.log") 2>&1

echo "started at $(date)"
echo "LOGDIR=$LOGDIR"
echo "XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
echo "DISPLAY=${DISPLAY:-}"
echo "APP_CMD=${APP_CMD[*]}"

tmux kill-session -t "$SESSION" 2>/dev/null || true
pkill -x monado-service 2>/dev/null || true
rm -f "$XDG_RUNTIME_DIR/monado_comp_ipc"
sleep 1

cat > "$LOGDIR/run_monado_windowed.sh" <<'MONADO_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

exec > >(tee -a "$LOGDIR/monado.log") 2>&1

echo "[monado windowed] starting at $(date)"
echo "[monado windowed] WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
echo "[monado windowed] DISPLAY=${DISPLAY:-}"

XRT_LOG=info \
XRT_COMPOSITOR_LOG=info \
XRT_COMPOSITOR_FORCE_WAYLAND=1 \
monado-service
MONADO_EOF

chmod +x "$LOGDIR/run_monado_windowed.sh"

tmux new-session -d -s "$SESSION" -n monado \
  "env LOGDIR='$LOGDIR' WAYLAND_DISPLAY='${WAYLAND_DISPLAY:-}' DISPLAY='${DISPLAY:-}' bash '$LOGDIR/run_monado_windowed.sh'"

echo "[wait socket]"
for i in $(seq 1 30); do
  if [ -S "$XDG_RUNTIME_DIR/monado_comp_ipc" ]; then
    echo "Monado IPC socket ready after ${i}s"
    break
  fi
  sleep 1
done

if [ ! -S "$XDG_RUNTIME_DIR/monado_comp_ipc" ]; then
  echo "FAILED: no Monado IPC socket"
  tmux capture-pane -t "$SESSION:0.0" -p -S -1000 > "$LOGDIR/monado_capture_failed.log" 2>/dev/null || true
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  exit 1
fi

echo "[run client]"
echo "${APP_CMD[@]}"

XR_LOADER_DEBUG=warn "${APP_CMD[@]}" > "$LOGDIR/client.log" 2>&1 || CLIENT_STATUS=$?
CLIENT_STATUS="${CLIENT_STATUS:-0}"

echo "client exit status: $CLIENT_STATUS"

tmux capture-pane -t "$SESSION:0.0" -p -S -2000 > "$LOGDIR/monado_capture.log" 2>/dev/null || true
tmux kill-session -t "$SESSION" 2>/dev/null || true

echo "finished at $(date)"
echo "LOGDIR=$LOGDIR"
