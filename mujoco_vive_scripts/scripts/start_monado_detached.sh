#!/usr/bin/env bash
# Start monado-service DETACHED from this terminal (Plan A + PTY).
#
# monado runs in a new session (setsid), immune to Ctrl+C from the
# launching terminal.  A persistent PTY is created via `script -qfc`
# fed by a self-opening FIFO to keep stdin alive (monado's init_epoll
# requires a valid epoll-able stdin fd).
#
# KDE environment (DISPLAY, WAYLAND_DISPLAY) is inherited.
#
# Pair with `stop_monado.sh` for graceful shutdown.

set -euo pipefail

# ---- Locations ----------------------------------------------------------
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/monado"
LOG_FILE="$STATE_DIR/monado-detached.log"
PID_FILE="$STATE_DIR/monado-detached.pid"
FIFO_FILE="$STATE_DIR/.stdin_feeder"
mkdir -p "$STATE_DIR"

# ---- Refuse double-launch ----------------------------------------------
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo 0)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[ERROR] monado-detached already running, PID $OLD_PID"
        echo "[INFO]  Use 'pixi run monado-stop' to stop first."
        exit 1
    fi
    rm -f "$PID_FILE"
fi

# ---- Display / env -------------------------------------------------------
export DISPLAY="${DISPLAY:-:1}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
echo "[INFO] DISPLAY=$DISPLAY  WAYLAND_DISPLAY=$WAYLAND_DISPLAY"

# ---- DP-3 state check (VIVE Pro DisplayPort) ----------------------------
# NVIDIA driver bug: after monado exits, the VIVE DP is left in a "locked"
# state (enabled=disabled) that only a reboot can clear.  Subsequent monado
# runs will stall at compositor init because vkAcquireXlibDisplayEXT hangs.
VIVE_DP="/sys/class/drm/card1-DP-3"
VIVE_ENABLED="unknown"
if [ -f "$VIVE_DP/enabled" ]; then
    VIVE_ENABLED=$(cat "$VIVE_DP/enabled" 2>/dev/null || echo "unknown")
fi
if [ "$VIVE_ENABLED" = "disabled" ]; then
    echo "[WARN] VIVE DP-3 is DISABLED (NVIDIA driver bug: display not"
    echo "       released from a previous monado session)."
    echo "       monado may stall at compositor init (VIVE green light OFF)."
    echo "       Only fix: system reboot."
    echo ""
    echo "       To confirm: if 'pixi run monado-status' shows 'IPC: MISSING'"
    echo "       and the log ends at 'compositor_check_and_prepare_xdev',"
    echo "       you have hit this bug."
fi

if ! lsusb 2>/dev/null | grep -qiE 'vive|htc|valve'; then
    echo "[WARN] VIVE Pro not found on USB.  Plug in USB+DP and retry."
fi

# ---- Cleanup leftovers --------------------------------------------------
pkill -9 monado-service hello_xr monado-cli 2>/dev/null || true
rm -f "${XDG_RUNTIME_DIR:-/tmp}/monado_comp_ipc" 2>/dev/null || true
export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json

# ---- Create persistent FIFO feeder -------------------------------------
# monado needs epoll-able stdin; /dev/null is unreliable (sometimes fails).
# `script -qfc` creates a PTY; the FIFO opened R+W keeps it alive forever.
rm -f "$FIFO_FILE"
mkfifo "$FIFO_FILE"
exec {FEEDER_FD}<> "$FIFO_FILE"
rm -f "$FIFO_FILE"   # unlink, fd stays alive

# ---- Launch --------------------------------------------------------------
setsid env \
    LIBC_FATAL_STDERR_=1 \
    XRT_LOG="${XRT_LOG:-info}" \
    PROBER_LOG="${PROBER_LOG:-info}" \
    VIVE_LOG="${VIVE_LOG:-info}" \
    XRT_COMPOSITOR_LOG="${XRT_COMPOSITOR_LOG:-info}" \
    DISPLAY="$DISPLAY" \
    WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
    script -qfc "monado-service" /dev/null \
        < "/dev/fd/${FEEDER_FD}" \
        > "$LOG_FILE" 2>&1 &
MONADO_PID=$!
echo "$MONADO_PID" > "$PID_FILE"
disown "$MONADO_PID" 2>/dev/null || true

# Close our local reference; child has its own.
eval "exec ${FEEDER_FD}>&-"

# ---- Health check (poll up to 30s for IPC socket) -----------------------
IPC="${XDG_RUNTIME_DIR:-/tmp}/monado_comp_ipc"
ALIVE=1
for _ in {1..300}; do
    if ! kill -0 "$MONADO_PID" 2>/dev/null; then
        ALIVE=0; break
    fi
    [ -S "$IPC" ] && break
    sleep 0.1
done

if [ "$ALIVE" -eq 0 ]; then
    echo "[ERROR] monado-service (PID $MONADO_PID) exited within 30s."
    echo "----- last 30 lines of $LOG_FILE -----"
    tail -30 "$LOG_FILE"
    echo "----------------------------------------"
    # Diagnose common failures
    if grep -q "init_epoll.*failed" "$LOG_FILE" 2>/dev/null; then
        echo "[HINT] 'init_epoll(stdin) failed' — try a system reboot."
    elif grep -q "no connectors\|No allowlisted" "$LOG_FILE" 2>/dev/null; then
        echo "[HINT] VIVE Pro not detected.  Plug in USB + DP cable and retry."
    elif grep -q "VK_ERROR_INITIALIZATION_FAILED" "$LOG_FILE" 2>/dev/null; then
        echo "[HINT] Vulkan swapchain creation failed.  Restart KWin or logout/login."
    elif [ "$VIVE_ENABLED" = "disabled" ]; then
        echo "[HINT] DP-3 is DISABLED.  This is the NVIDIA driver bug:"
        echo "       the VIVE display was not released after a previous monado run."
        echo "       Only fix: reboot.  (Confirmed working after fresh reboot.)"
    elif ! grep -q "Selected.*Direct-Mode" "$LOG_FILE" 2>/dev/null; then
        echo "[HINT] monado never selected a compositor backend.  Likely the same"
        echo "       NVIDIA driver DP-3 stuck-disabled issue.  Try: reboot."
    fi
    rm -f "$PID_FILE"
    exit 1
fi

if [ ! -S "$IPC" ]; then
    echo "[ERROR] monado alive but no IPC after 30s (stuck in Vulkan init)."
    echo "       Try: restart KWin, logout/login, or reboot."
    tail -10 "$LOG_FILE"
    rm -f "$PID_FILE"
    kill -KILL "$MONADO_PID" 2>/dev/null
    exit 1
fi

# ---- Success ------------------------------------------------------------
echo "[OK] monado-service running, PID $MONADO_PID"
echo "      DISPLAY=$DISPLAY  WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
echo "      Stop:  pixi run monado-stop"
echo "      Status: pixi run monado-status"
echo "      Log:   tail -f $LOG_FILE"
echo ""
echo "      Ctrl+C in any terminal does NOT affect monado (setsid)."