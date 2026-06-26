#!/usr/bin/env bash
set -Eeuo pipefail

SESSION="vive_helloxr_kde"
BASE_DIR="${BASE_DIR:-/home/zorro/projects/vive_pro_vr/script_test}"
LOG_ROOT="${LOG_ROOT:-$BASE_DIR/logs}"
RUN_SECONDS="${RUN_SECONDS:-90}"
HMD_CONNECTOR="${HMD_CONNECTOR:-DP-3}"
XR_RUNTIME_JSON="${XR_RUNTIME_JSON:-/usr/share/openxr/1/openxr_monado.json}"
SOCKET_TIMEOUT="${SOCKET_TIMEOUT:-40}"
KILL_STEAMVR="${KILL_STEAMVR:-0}"
TRY_FORCE_WAYLAND_DIRECT="${TRY_FORCE_WAYLAND_DIRECT:-0}"

MODE="run"

usage() {
  cat <<USAGE
Usage:
  $0 [options]

Options:
  --duration SEC              hello_xr run time, default: ${RUN_SECONDS}
  --connector NAME            Wayland/DRM connector, default: ${HMD_CONNECTOR}
                              Use "auto" to let Monado choose.
  --force-wayland-direct      Also set XRT_COMPOSITOR_FORCE_WAYLAND_DIRECT=1
  --kill-steamvr              Also kill SteamVR related processes before/after
  --check-only                Only check environment
  --cleanup-only              Only cleanup residual processes/socket
  -h, --help                  Show this help

Examples:
  $0
  $0 --duration 60
  $0 --connector DP-3
  $0 --connector auto
  $0 --cleanup-only
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration)
      RUN_SECONDS="$2"
      shift 2
      ;;
    --connector)
      HMD_CONNECTOR="$2"
      shift 2
      ;;
    --force-wayland-direct)
      TRY_FORCE_WAYLAND_DIRECT=1
      shift
      ;;
    --kill-steamvr)
      KILL_STEAMVR=1
      shift
      ;;
    --check-only)
      MODE="check"
      shift
      ;;
    --cleanup-only)
      MODE="cleanup"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 2
      ;;
  esac
done

cleanup_processes() {
  tmux kill-session -t "$SESSION" 2>/dev/null || true

  # 只按精确进程名杀，避免 pkill -f 误杀脚本自身。
  pkill -x hello_xr 2>/dev/null || true
  pkill -x monado-service 2>/dev/null || true

  if [[ "$KILL_STEAMVR" == "1" ]]; then
    pkill -f "vrmonitor|vrserver|vrcompositor|vrdashboard|steamvr|steamvrwebhelper" 2>/dev/null || true
  fi

  if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
    rm -f "$XDG_RUNTIME_DIR/monado_comp_ipc" 2>/dev/null || true
  fi
}

run_checks() {
  echo "== Session =="
  echo "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-}"
  echo "XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-}"
  echo "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
  echo "DISPLAY=${DISPLAY:-}"
  echo "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  echo

  echo "== Commands =="
  for cmd in tmux monado-service hello_xr wayland-info; do
    if command -v "$cmd" >/dev/null 2>&1; then
      echo "[OK] $cmd -> $(command -v "$cmd")"
    else
      echo "[MISSING] $cmd"
      return 1
    fi
  done
  echo

  echo "== Runtime =="
  if [[ -f "$XR_RUNTIME_JSON" ]]; then
    echo "[OK] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
  else
    echo "[MISSING] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
    return 1
  fi
  echo

  echo "== Wayland DRM lease =="
  if timeout 5s wayland-info 2>/dev/null | grep -qiE "wp_drm_lease|drm_lease|lease"; then
    echo "[OK] Wayland compositor exposes DRM lease protocol"
    timeout 5s wayland-info 2>/dev/null | grep -iE "wp_drm_lease|drm_lease|lease" || true
  else
    echo "[ERROR] No DRM lease protocol found. Are you in KDE Plasma Wayland?"
    return 1
  fi
  echo

  echo "== DRM connector guess =="
  shopt -s nullglob
  local found=0
  for c in /sys/class/drm/card*-"$HMD_CONNECTOR"; do
    found=1
    echo "connector=$c"
    echo "status=$(cat "$c/status" 2>/dev/null || echo unknown)"
    echo "modes:"
    cat "$c/modes" 2>/dev/null | head -10 || true
    if [[ -f "$c/non_desktop" ]]; then
      echo "non_desktop=$(cat "$c/non_desktop" 2>/dev/null || echo unknown)"
    fi
    echo
  done

  if [[ "$HMD_CONNECTOR" != "auto" && "$found" == "0" ]]; then
    echo "[WARN] No /sys/class/drm/card*-$HMD_CONNECTOR found."
    echo "       If your connector name differs, run with --connector auto or --connector DP-X."
  fi
}

if [[ "$MODE" == "cleanup" ]]; then
  cleanup_processes
  echo "Cleanup done."
  exit 0
fi

if [[ "$MODE" == "check" ]]; then
  run_checks
  exit $?
fi

mkdir -p "$LOG_ROOT"
LOGDIR="$LOG_ROOT/helloxr_kde_vr_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOGDIR"
ln -sfn "$LOGDIR" "$LOG_ROOT/latest_helloxr_kde_vr"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

exec > >(tee -a "$LOGDIR/driver.log") 2>&1

trap cleanup_processes EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "started at $(date)"
echo "LOGDIR=$LOGDIR"
echo "RUN_SECONDS=$RUN_SECONDS"
echo "HMD_CONNECTOR=$HMD_CONNECTOR"
echo "XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo

echo "== Pre-check =="
run_checks || {
  echo "[ERROR] Environment check failed."
  exit 1
}

echo
echo "== Cleanup before run =="
cleanup_processes
sleep 1

cat > "$LOGDIR/env.sh" <<ENV_EOF
export XR_RUNTIME_JSON=$(printf '%q' "$XR_RUNTIME_JSON")
export XDG_RUNTIME_DIR=$(printf '%q' "$XDG_RUNTIME_DIR")
export WAYLAND_DISPLAY=$(printf '%q' "${WAYLAND_DISPLAY:-}")
export DISPLAY=$(printf '%q' "${DISPLAY:-}")
export DBUS_SESSION_BUS_ADDRESS=$(printf '%q' "${DBUS_SESSION_BUS_ADDRESS:-}")
export HMD_CONNECTOR=$(printf '%q' "$HMD_CONNECTOR")
export RUN_SECONDS=$(printf '%q' "$RUN_SECONDS")
export TRY_FORCE_WAYLAND_DIRECT=$(printf '%q' "$TRY_FORCE_WAYLAND_DIRECT")
export LOGDIR=$(printf '%q' "$LOGDIR")
export PATH=$(printf '%q' "$PATH")
ENV_EOF

cat > "$LOGDIR/run_monado.sh" <<'MONADO_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/env.sh"

exec > >(tee -a "$LOGDIR/monado.log") 2>&1

echo "[monado] starting at $(date)"
echo "[monado] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "[monado] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "[monado] WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
echo "[monado] DISPLAY=${DISPLAY:-}"
echo "[monado] HMD_CONNECTOR=$HMD_CONNECTOR"

export XR_RUNTIME_JSON
export XDG_RUNTIME_DIR

# KDE Plasma Wayland 下保留 WAYLAND_DISPLAY/DISPLAY。
# 不要 unset DISPLAY/WAYLAND_DISPLAY，否则会退回 TTY/VkDisplay 那类路线。
if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
  export WAYLAND_DISPLAY
fi
if [[ -n "${DISPLAY:-}" ]]; then
  export DISPLAY
fi
if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  export DBUS_SESSION_BUS_ADDRESS
fi

export XRT_LOG=debug
export XRT_COMPOSITOR_LOG=debug
export XRT_COMPOSITOR_PRINT_MODES=1

# 强制走 Wayland compositor 路线，避免 NVIDIA Xlib direct / TTY direct。
export XRT_COMPOSITOR_FORCE_WAYLAND=1

# 指定 HMD connector。若设为 auto，则让 Monado 自动选择。
if [[ "${HMD_CONNECTOR}" != "auto" && -n "${HMD_CONNECTOR}" ]]; then
  export XRT_COMPOSITOR_WAYLAND_CONNECTOR="$HMD_CONNECTOR"
fi

# 某些 Monado/发行版组合需要这个；你的旧版本曾经会报 wayland_direct/direct_wayland 名称不匹配，
# 所以默认不开。需要时运行主脚本加 --force-wayland-direct。
if [[ "${TRY_FORCE_WAYLAND_DIRECT}" == "1" ]]; then
  export XRT_COMPOSITOR_FORCE_WAYLAND_DIRECT=1
fi

set +e
monado-service
status=$?
set -e

echo "[monado] exited with status=$status at $(date)"
exit "$status"
MONADO_EOF

cat > "$LOGDIR/run_hello.sh" <<'HELLO_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/env.sh"

exec > >(tee -a "$LOGDIR/hello_xr.log") 2>&1

echo "[hello] starting at $(date)"
echo "[hello] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "[hello] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "[hello] socket:"
ls -l "$XDG_RUNTIME_DIR/monado_comp_ipc" || true

export XR_RUNTIME_JSON
export XDG_RUNTIME_DIR
if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
  export WAYLAND_DISPLAY
fi
if [[ -n "${DISPLAY:-}" ]]; then
  export DISPLAY
fi
if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  export DBUS_SESSION_BUS_ADDRESS
fi

echo
echo "[hello] help:"
hello_xr -h || true

echo
echo "[hello] running:"
echo "hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v"

set +e
XR_LOADER_DEBUG=all \
timeout --foreground "${RUN_SECONDS}s" \
hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v
status=$?
set -e

echo "[hello] exit status=$status at $(date)"
exit 0
HELLO_EOF

chmod +x "$LOGDIR/run_monado.sh" "$LOGDIR/run_hello.sh"

echo
echo "== Start Monado in tmux =="
tmux new-session -d -s "$SESSION" -n monado "bash '$LOGDIR/run_monado.sh'"

echo
echo "== Wait for Monado IPC socket =="
for i in $(seq 1 "$SOCKET_TIMEOUT"); do
  if [[ -S "$XDG_RUNTIME_DIR/monado_comp_ipc" ]]; then
    echo "Monado IPC socket ready after ${i}s" | tee "$LOGDIR/result.txt"
    break
  fi

  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "FAILED: tmux/monado session exited before socket was created" | tee "$LOGDIR/result.txt"
    break
  fi

  sleep 1
done

tmux capture-pane -t "$SESSION:0.0" -p -S -3000 > "$LOGDIR/monado_capture_before_hello.log" 2>/dev/null || true

if [[ ! -S "$XDG_RUNTIME_DIR/monado_comp_ipc" ]]; then
  echo "FAILED: no Monado IPC socket" | tee -a "$LOGDIR/result.txt"
  echo "See logs: $LOGDIR"
  exit 1
fi

echo
echo "== Start hello_xr =="
tmux split-window -h -t "$SESSION:0.0" "bash '$LOGDIR/run_hello.sh'"

echo "hello_xr started, waiting ${RUN_SECONDS}s..."
sleep "$RUN_SECONDS"
sleep 3

echo
echo "== Capture logs =="
tmux capture-pane -t "$SESSION:0.0" -p -S -5000 > "$LOGDIR/monado_capture_after_hello.log" 2>/dev/null || true
tmux capture-pane -t "$SESSION:0.1" -p -S -5000 > "$LOGDIR/hello_xr_capture.log" 2>/dev/null || true

{
  echo
  echo "=== summary ==="
  grep -hEi "Wayland|direct|lease|Will use display|VIVE|2880x1600|Created listening|BEGIN_SESSION|active app|client|frame|layer|projection|session|state|visible|focused|running|ERROR|failed|GraphicsPlugin|RuntimeName|Selected devices|exit status" \
    "$LOGDIR"/monado.log \
    "$LOGDIR"/monado_capture_after_hello.log \
    "$LOGDIR"/hello_xr.log \
    "$LOGDIR"/hello_xr_capture.log 2>/dev/null | tail -n 500 || true
} | tee -a "$LOGDIR/result.txt"

echo
echo "== Cleanup after run =="
cleanup_processes

echo "finished at $(date)" | tee -a "$LOGDIR/result.txt"
echo "LOGDIR=$LOGDIR"
