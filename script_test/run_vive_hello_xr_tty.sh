#!/usr/bin/env bash
set -u

SESSION="vive_xr_test"
LOGDIR="$HOME/vive_openxr_test_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOGDIR"

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
unset DISPLAY
unset WAYLAND_DISPLAY

echo "Logs will be saved to: $LOGDIR"

tmux kill-session -t "$SESSION" 2>/dev/null || true
pkill -x hello_xr 2>/dev/null || true
pkill -x monado-service 2>/dev/null || true
rm -f "/run/user/$(id -u)/monado_comp_ipc"

MONADO_CMD='
export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
unset DISPLAY
unset WAYLAND_DISPLAY
XRT_LOG=debug \
XRT_COMPOSITOR_LOG=debug \
XRT_COMPOSITOR_PRINT_MODES=1 \
XRT_COMPOSITOR_FORCE_VK_DISPLAY=2 \
XRT_COMPOSITOR_DESIRED_MODE=0 \
monado-service
'

tmux new-session -d -s "$SESSION" "bash -lc '$MONADO_CMD'"

# Capture tmux pane output to log file.
tmux pipe-pane -t "$SESSION" "cat > '$LOGDIR/monado.log'"

echo "Waiting for Monado IPC socket..."
for i in $(seq 1 20); do
  if [ -S "/run/user/$(id -u)/monado_comp_ipc" ]; then
    echo "Monado IPC socket is ready." | tee "$LOGDIR/result.txt"
    break
  fi
  sleep 1
done

if [ ! -S "/run/user/$(id -u)/monado_comp_ipc" ]; then
  echo "monado-service did not create IPC socket" | tee "$LOGDIR/result.txt"
  tmux capture-pane -t "$SESSION" -p -S -300 > "$LOGDIR/monado_capture.log" 2>/dev/null || true
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  exit 1
fi

echo "Starting hello_xr..." | tee -a "$LOGDIR/result.txt"

timeout 30s hello_xr -G Vulkan > "$LOGDIR/hello_xr.log" 2>&1
HELLO_STATUS=$?

# Some builds use -g instead of -G; try fallback only if command-line parsing failed.
if grep -qiE "unknown option|usage" "$LOGDIR/hello_xr.log"; then
  echo "Retrying hello_xr with -g Vulkan..." | tee -a "$LOGDIR/result.txt"
  timeout 30s hello_xr -g Vulkan > "$LOGDIR/hello_xr.log" 2>&1
  HELLO_STATUS=$?
fi

echo "hello_xr exit status: $HELLO_STATUS" | tee -a "$LOGDIR/result.txt"

sleep 2

tmux capture-pane -t "$SESSION" -p -S -500 > "$LOGDIR/monado_capture.log" 2>/dev/null || true
tmux kill-session -t "$SESSION" 2>/dev/null || true

echo "Done. Logs saved to:"
echo "$LOGDIR"
