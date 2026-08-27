#!/usr/bin/env bash
# Suspend/resume the Force Dimension omega USB devices (idVendor 1451).
#
# The omega.x firmware disturbs the shared xHCI controller every ~5.0s even
# when no software has the devices open (see
# mujoco_vive_scripts/vr_stutter_omega_analysis.md): ~0.9s of video-isoch
# frame loss on the capture boxes and VIVE tracking.  Forcing the devices
# into USB suspend while NOT teleoperating removes the disturbance.
#
# Usage:
#   omega-usb-suspend.sh off    # suspend both omega devices (needs sudo)
#   omega-usb-suspend.sh on     # resume before starting teleop (needs sudo)
#   omega-usb-suspend.sh status # show current power state (no sudo)
set -u

find_devices() {
    for d in /sys/bus/usb/devices/*; do
        [ -e "$d/idVendor" ] || continue
        [ "$(cat "$d/idVendor" 2>/dev/null)" = "1451" ] && echo "${d##*/}"
    done
}

case "${1:-status}" in
    off)
        devs=$(find_devices)
        if [ -z "$devs" ]; then
            echo "[omega-usb-suspend] no Force Dimension devices found."
            exit 1
        fi
        script=""
        for d in $devs; do
            script="$script echo suspend > /sys/bus/usb/devices/$d/power/level;"
        done
        echo "[omega-usb-suspend] suspending: $devs (sudo required)"
        sudo sh -c "$script"
        ;;
    on)
        devs=$(find_devices)
        for d in $devs; do
            sudo sh -c "echo on > /sys/bus/usb/devices/$d/power/level"
        done
        echo "[omega-usb-suspend] resumed: $devs"
        ;;
    status)
        for d in $(find_devices); do
            echo "$d: $(cat /sys/bus/usb/devices/$d/power/level 2>/dev/null)"
        done
        ;;
esac
