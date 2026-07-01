#!/usr/bin/env bash
# Standalone Xwayland on :99, dedicated for monado-service.
#
# Plan C2 from the Xwayland crash analysis: monado's libxcb traffic is
# diverted to this private Xwayland instead of KDE's :1.  A crash here
# only kills monado, not the rest of the desktop.
#
# Idempotent: if :99 is already alive (socket + process), exit 0.
# This Xwayland is launched with `-ac -noreset`:
#   -ac       accept any local client (single-user dev box)
#   -noreset  don't reset on last-client-exit (so it survives monado restarts)
# Note: Xwayland has no `-screen` flag (that's Xorg); screen size is implied
# by the connected Wayland output's geometry, which gives us a minimal
# virtual surface automatically.

set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/monado"
PID_FILE="$STATE_DIR/xwayland-99.pid"
LOG_FILE="$STATE_DIR/xwayland-99.log"
XAUTH_FILE="$STATE_DIR/xwayland-99.xauth"
SOCKET="/tmp/.X11-unix/X99"
mkdir -p "$STATE_DIR"

# ---- Idempotent check -----------------------------------------------------
if [ -S "$SOCKET" ] && pgrep -f 'Xwayland :99' > /dev/null 2>&1; then
    XWAYLAND_PID=$(pgrep -f 'Xwayland :99' | head -1)
    echo "[OK] Xwayland :99 already running, PID $XWAYLAND_PID."
    echo "$XWAYLAND_PID" > "$PID_FILE"  # refresh
    exit 0
fi

# ---- Cleanup any stale state --------------------------------------------
rm -f "$SOCKET" "$PID_FILE"
pkill -9 -f 'Xwayland :99' 2>/dev/null || true
sleep 0.2

# ---- Generate xauth (one-time, then reused) ------------------------------
# xauth is required by -auth path even with -ac; we generate an MIT cookie.
if [ ! -f "$XAUTH_FILE" ] || [ ! -s "$XAUTH_FILE" ]; then
    : > "$XAUTH_FILE"
    if ! xauth -f "$XAUTH_FILE" generate :99 MIT-MAGIC-COOKIE-1 2>/dev/null; then
        echo "  untrusted" > "$XAUTH_FILE"
    fi
    chmod 600 "$XAUTH_FILE"
fi
AUTH_ARG="-auth $XAUTH_FILE"

# ---- Launch Xwayland :99 standalone in a new session --------------------
# Xwayland as a Wayland client still needs WAYLAND_DISPLAY (it talks to
# KWin's wayland-0 to be composited), but it does NOT touch our :1 Xwayland.
#
# Note on flags: Xwayland does NOT support `-screen` (that's Xorg).  Screen
# size is governed by the Wayland output's geometry; with no real output
# connected, we get a minimal virtual surface automatically.
#
# We wrap with `script -qfc` so Xwayland gets a PTY (some X clients expect
# one for stdin/epoll — same reason monado-service uses it).
echo "[INFO] Starting Xwayland :99 (private, -ac -noreset)..."

setsid env \
    WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}" \
    XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
    script -qfc "Xwayland :99 $AUTH_ARG -ac -noreset" \
        /dev/null < /dev/null > "$LOG_FILE" 2>&1 &

# Capture the wrapper PID (script process); Xwayland is its child.
XPID=$!
echo "$XPID" > "$PID_FILE"
disown "$XPID" 2>/dev/null || true

# ---- Wait for :99 socket + xdpyinfo probe (max 5s) ---------------------
READY=0
for _ in {1..50}; do
    if [ -S "$SOCKET" ] && DISPLAY=:99 xdpyinfo >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 0.1
done

if [ "$READY" -eq 0 ]; then
    echo "[ERROR] Xwayland :99 failed to become ready within 5s."
    echo "----- last 25 lines of $LOG_FILE -----"
    tail -25 "$LOG_FILE" || true
    echo "----------------------------------------"
    rm -f "$PID_FILE"
    pkill -KILL -f 'Xwayland :99' 2>/dev/null || true
    rm -f "$SOCKET"
    exit 1
fi

# Find the actual Xwayland PID (child of script wrapper) for nicer status output
REAL_XWAYLAND_PID=$(pgrep -f 'Xwayland :99' | head -1 || echo $XPID)
echo "$REAL_XWAYLAND_PID" > "$PID_FILE"

echo "[OK] Xwayland :99 ready, PID $REAL_XWAYLAND_PID"
echo "     Stop with:  pixi run monado-stop"
echo "     Log:        $LOG_FILE"