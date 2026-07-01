#!/usr/bin/env bash
# Stop monado-service gracefully, then auto-recover KDE's Xwayland :1
# if monado's teardown triggered the known crash race.
#
# Order: monado-stop → wait for Xwayland crash → trigger KWin recovery.

set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/monado"
PID_FILE="$STATE_DIR/monado-detached.pid"

_wait_exit() {
    local pid="$1" max_steps="$2"
    for _ in $(seq 1 "$max_steps"); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.2
    done
    return 1
}

# ---- Stop monado ---------------------------------------------------------
if [ -f "$PID_FILE" ]; then
    MPID=$(cat "$PID_FILE")
    if kill -0 "$MPID" 2>/dev/null; then
        echo "[INFO] Sending SIGINT to monado-service (PID $MPID)..."
        kill -INT "$MPID"
        if _wait_exit "$MPID" 50; then   # 10s
            echo "[OK] monado exited on SIGINT."
        else
            echo "[WARN] Escalating to SIGTERM..."
            kill -TERM "$MPID"
            if _wait_exit "$MPID" 25; then   # 5s
                echo "[OK] monado exited on SIGTERM."
            else
                echo "[ERROR] Escalating to SIGKILL..."
                kill -KILL "$MPID"
                sleep 0.5
            fi
        fi
    else
        echo "[INFO] PID $MPID already dead."
    fi
    rm -f "$PID_FILE"
else
    echo "[INFO] No PID file — monado may be running via some other method."
fi

# ---- Auto-recover KDE Xwayland :1 ---------------------------------------
#
# monado's teardown (closing its libxcb connection to KDE's :1 Xwayland)
# races with Xwayland's Wayland event loop, causing Xwayland to segfault.
# KWin's XwaylandCrashPolicy=Restart should restart it, but we nudge it
# via DBus reconfigure to speed things up.
echo ""
echo "[recover] Checking KDE :1 Xwayland..."

# Wait up to 5s for the potential crash to manifest.
XW_ALIVE=1
for _ in {1..25}; do
    if pgrep -f 'Xwayland :1' > /dev/null 2>&1; then
        XW_ALIVE=1
        break
    fi
    XW_ALIVE=0
    sleep 0.2
done

if [ "$XW_ALIVE" -eq 1 ]; then
    echo "[recover] Xwayland :1 is still alive — no crash detected."
    echo "[OK] done."
    exit 0
fi

echo "[recover] Xwayland :1 has CRASHED (monado teardown race)."
echo "[recover] KWin XwaylandCrashPolicy=Restart is configured; nudging..."
gdbus call --session --dest org.kde.KWin --object-path /KWin \
    --method org.kde.KWin.reconfigure > /dev/null 2>&1 || true

# Poll up to 15s for Xwayland :1 to come back.
for _ in {1..75}; do
    if pgrep -f 'Xwayland :1' > /dev/null 2>&1; then
        NEW_PID=$(pgrep -f 'Xwayland :1' | head -1)
        echo "[recover] Xwayland :1 restarted, PID $NEW_PID."
        echo "[OK] done."
        exit 0
    fi
    sleep 0.2
done

echo "[recover] Xwayland :1 did NOT come back within 15s."
echo "[recover] Try manually:  pixi run kde-recover"
echo "[recover] Or logout/login, or reboot."
echo "[OK] done (Xwayland may still be down)."