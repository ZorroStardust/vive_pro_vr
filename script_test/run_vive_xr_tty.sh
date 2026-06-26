#!/usr/bin/env bash
set -u

SESSION="vive_xr_test"
LOGDIR="$HOME/vive_openxr_test_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOGDIR"

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
unset DISPLAY
unset WAYLAND_DISPLAY

echo "started at $(date)" | tee "$LOGDIR/result.txt"
echo "Logs: $LOGDIR" | tee -a "$LOGDIR/result.txt"

tmux kill-session -t "$SESSION" 2>/dev/null || true
pkill -x hello_xr 2>/dev/null || true
pkill -x monado-service 2>/dev/null || true
rm -f "$XDG_RUNTIME_DIR/monado_comp_ipc"

MONADO_CMD='
export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
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
sleep 1
tmux pipe-pane -t "$SESSION" "cat > '$LOGDIR/monado.log'"

echo "waiting for Monado IPC socket..." | tee -a "$LOGDIR/result.txt"

for i in $(seq 1 30); do
  if [ -S "$XDG_RUNTIME_DIR/monado_comp_ipc" ]; then
    echo "Monado IPC socket is ready after ${i}s." | tee -a "$LOGDIR/result.txt"
    break
  fi

  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session exited before IPC socket was created." | tee -a "$LOGDIR/result.txt"
    break
  fi

  sleep 1
done

tmux capture-pane -t "$SESSION" -p -S -1000 > "$LOGDIR/monado_capture.log" 2>/dev/null || true

if [ ! -S "$XDG_RUNTIME_DIR/monado_comp_ipc" ]; then
  echo "monado-service did not create IPC socket." | tee -a "$LOGDIR/result.txt"
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  exit 1
fi

echo "starting hello_xr..." | tee -a "$LOGDIR/result.txt"

hello_xr -h > "$LOGDIR/hello_xr_help.log" 2>&1 || true

XR_LOADER_DEBUG=all \
timeout --foreground 60s hello_xr -g Vulkan > "$LOGDIR/hello_xr.log" 2>&1

HELLO_STATUS=$?
echo "hello_xr exit status: $HELLO_STATUS" | tee -a "$LOGDIR/result.txt"

sleep 2
tmux capture-pane -t "$SESSION" -p -S -1000 > "$LOGDIR/monado_capture_after_hello.log" 2>/dev/null || true
tmux kill-session -t "$SESSION" 2>/dev/null || true

echo "finished at $(date)" | tee -a "$LOGDIR/result.txt"
echo "$LOGDIR"
