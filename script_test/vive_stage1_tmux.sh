#!/usr/bin/env bash
set -Eeuo pipefail

SESSION="vive_stage1"
RUN_SECONDS="${RUN_SECONDS:-90}"
LOGDIR="$HOME/vive_openxr_test_$(date +%Y%m%d_%H%M%S)"

mkdir -p "$LOGDIR"
ln -sfn "$LOGDIR" "$HOME/vive_openxr_latest"

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
unset DISPLAY
unset WAYLAND_DISPLAY

exec > >(tee -a "$LOGDIR/driver.log") 2>&1

echo "started at $(date)"
echo "LOGDIR=$LOGDIR"
echo "XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"

tmux kill-session -t "$SESSION" 2>/dev/null || true
pkill -x hello_xr 2>/dev/null || true
pkill -x monado-service 2>/dev/null || true
rm -f "$XDG_RUNTIME_DIR/monado_comp_ipc"
sleep 1

cat > "$LOGDIR/run_monado.sh" <<'MONADO_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
unset DISPLAY
unset WAYLAND_DISPLAY

exec > >(tee -a "$LOGDIR/monado.log") 2>&1

echo "[monado] starting at $(date)"
echo "[monado] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "[monado] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"

XRT_LOG=debug \
XRT_COMPOSITOR_LOG=debug \
XRT_COMPOSITOR_PRINT_MODES=1 \
XRT_COMPOSITOR_FORCE_VK_DISPLAY=2 \
XRT_COMPOSITOR_DESIRED_MODE=0 \
monado-service

echo "[monado] exited with $?"
MONADO_EOF

cat > "$LOGDIR/run_hello.sh" <<'HELLO_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
unset DISPLAY
unset WAYLAND_DISPLAY

exec > >(tee -a "$LOGDIR/hello_xr.log") 2>&1

echo "[hello] starting at $(date)"
echo "[hello] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "[hello] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "[hello] socket:"
ls -l "$XDG_RUNTIME_DIR/monado_comp_ipc" || true

echo
echo "[hello] help:"
hello_xr -h || true

echo
echo "[hello] running:"
echo "hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v"

XR_LOADER_DEBUG=all \
timeout --foreground "${RUN_SECONDS:-90}s" \
hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v

echo "[hello] exit status: $?"
HELLO_EOF

chmod +x "$LOGDIR/run_monado.sh" "$LOGDIR/run_hello.sh"

echo "[start tmux monado]"
tmux new-session -d -s "$SESSION" -n monado "env LOGDIR='$LOGDIR' bash '$LOGDIR/run_monado.sh'"

echo "[wait socket]"
for i in $(seq 1 40); do
  if [ -S "$XDG_RUNTIME_DIR/monado_comp_ipc" ]; then
    echo "Monado IPC socket ready after ${i}s" | tee "$LOGDIR/result.txt"
    break
  fi

  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "FAILED: tmux/monado session exited before socket was created" | tee "$LOGDIR/result.txt"
    break
  fi

  sleep 1
done

if [ ! -S "$XDG_RUNTIME_DIR/monado_comp_ipc" ]; then
  echo "FAILED: no Monado IPC socket" | tee -a "$LOGDIR/result.txt"
  tmux capture-pane -t "$SESSION:0.0" -p -S -3000 > "$LOGDIR/monado_capture_failed.log" 2>/dev/null || true
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  exit 1
fi

echo "[start tmux hello]"
tmux split-window -h -t "$SESSION:0.0" "env LOGDIR='$LOGDIR' RUN_SECONDS='$RUN_SECONDS' bash '$LOGDIR/run_hello.sh'"

echo "hello_xr started, waiting ${RUN_SECONDS}s..."
sleep "$RUN_SECONDS"
sleep 5

tmux capture-pane -t "$SESSION:0.0" -p -S -4000 > "$LOGDIR/monado_capture_after_hello.log" 2>/dev/null || true
tmux capture-pane -t "$SESSION:0.1" -p -S -4000 > "$LOGDIR/hello_xr_capture.log" 2>/dev/null || true

{
  echo
  echo "=== summary ==="
  grep -hEi "Will use display|2880x1600|Created listening|BEGIN_SESSION|active app|client|frame|layer|projection|session|state|visible|focused|running|error|failed|exit status|GraphicsPlugin" \
    "$LOGDIR"/monado.log \
    "$LOGDIR"/monado_capture_after_hello.log \
    "$LOGDIR"/hello_xr.log \
    "$LOGDIR"/hello_xr_capture.log 2>/dev/null | tail -n 400 || true
} | tee -a "$LOGDIR/result.txt"

tmux kill-session -t "$SESSION" 2>/dev/null || true

echo "finished at $(date)" | tee -a "$LOGDIR/result.txt"
echo "LOGDIR=$LOGDIR"
