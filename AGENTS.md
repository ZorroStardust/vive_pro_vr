# AGENTS.md — vive_pro_vr (MuJoCo + VIVE Pro + Monado)

## Repo structure

- `mujoco_vive_scripts/` — primary Python package. Active development lives here.
- `script_test/` — shell scripts for raw Monado/VIVE TTY testing, diagnostics, and log snapshots.
- `stage1.md` — debug journal documenting the full hardware/software setup chain.

No CI, no tests, no lint config. The repo is a workspace for prototyping, not a shipped library. Git is used locally for history; no remote is configured.

## Python environment

Uses **[pixi](https://pixi.sh)** for dependency management. Python 3.13.x is managed as a conda dependency.

```bash
pixi install          # create env and install all deps (conda + PyPI)
```

The `mujoco_vive_scripts` package is installed in editable mode automatically. `mvs-*` console scripts are available inside the pixi env.

System deps (Monado, GLFW, Vulkan, etc.) are not managed by pixi. Install them once:

```bash
pixi run install-system-deps
```

## Key commands (all via pixi)

| Task | Command |
|------|---------|
| Install env | `pixi install` |
| List GLFW monitors | `pixi run list-monitors` |
| Offscreen stereo render | `pixi run save-frames` (outputs `out/left.png right.png sbs.png`) |
| SBS windowed | `pixi run sbs-windowed` |
| SBS fullscreen on monitor N | `MONITOR=N pixi run sbs-fullscreen` |
| Start Monado (Wayland windowed, **attached to this terminal**) | `pixi run start-monado` |
| Start Monado **detached** (`setsid` + PTY, Ctrl+C-safe, recommended) | `pixi run monado-detached` |
| Stop detached Monado (SIGINT → 10s wait → SIGTERM → 5s wait → SIGKILL) | `pixi run monado-stop` |
| Show detached Monado status + IPC + log tail | `pixi run monado-status` |
| Try to restart crashed KDE :1 Xwayland (DBus reconfigure) | `pixi run kde-recover` |
| Test hello_xr against Monado | `pixi run test-openxr` |
| OpenXR env check | `pixi run openxr-check` |
| OpenXR pink world (left/right pure color) | `pixi run xr-pink-world` |
| OpenXR MuJoCo stereo (comfort controls) | `pixi run xr-mujoco-opengl` |
| OpenXR crosshair calibration tool | `pixi run xr-crosshair` |
| OpenXR surgical robot stereo preview | `pixi run xr-surgical` |
| Endoscope live stereo video → HMD | `pixi run xr-live-video` (auto-detects the two Cypress `04b4:00f9` capture boxes, skipping the VIVE HMD camera; override with `--left-dev/--right-dev`, YUYV 1920x1080@60) |
| Endoscope live video, desktop window | `pixi run xr-live-video-screen` |
| Endoscope GL path test (no cameras) | `pixi run xr-live-video-testcard` |
| Shell in pixi env | `pixi shell` |
| Full TTY direct-mode pipeline | `~/vive_stage1_tmux.sh` (from `script_test/`) |

## Hardware/OS invariants

- **Ubuntu 24.04**, GNOME Wayland (was KDE Wayland; both tested).
- **NVIDIA RTX 5060 Ti**, driver 580.x.
- **HTC VIVE Pro / P110**, connected via Link Box.
- **Monado** is the OpenXR runtime: `XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json`.
- VIVE shows as DRM `card1-DP-3` (non-desktop=1), 2880x1600@90Hz.
- **VkDisplay direct-mode requires TTY** — GNOME Wayland DRM lease doesn't work. Switch to TTY3 with `sudo chvt 3`.

## Monado specifics

- VIVE Vulkan display index is **2**: `XRT_COMPOSITOR_FORCE_VK_DISPLAY=2`.
- Monado **must have a PTY** for stdin epoll (`init_epoll`). `/dev/null` works
  intermittently; `script -qfc` with a self-opening FIFO (R+W) is the reliable
  approach — used by `start_monado_detached.sh`.
- IPC socket: `/run/user/$(id -u)/monado_comp_ipc`. Delete stale sockets on failure.
- **Base stations and controllers must be OFF** during display-only testing. When on, Lighthouse pulse reports can overflow Monado's buffer.
- Always check for `1 active app session(s)` in Monado log to confirm client connection succeeded.

## Process management gotchas

- Never `pkill -f hello_xr` — it matches the script filename and kills the script. Use `pkill -x hello_xr`.
- Never `sudo` OpenXR clients — the IPC socket is per-user under `/run/user/$UID/`.
- **Monado needs a PTY for stdin**: `monado-service` calls `epoll_ctl(STDIN_FILENO, ...)` inside
  its main loop (init_epoll).  Without a valid epoll-able fd, monado exits immediately with
  "init_epoll failed".  `/dev/null` is unreliable as stdin (succeeds only intermittently).
  **`start_monado_detached.sh` uses `script -qfc` to create a PTY, fed by a self-opening FIFO
  (R+W) to keep it alive forever.**  This is the only approach that works reliably.
- **Ctrl+C chain**: when monado runs in a detached session (`setsid`), Ctrl+C in the
  launching terminal does NOT propagate to monado.  xr-pink-world Ctrl+C only kills the
  client; monado stays alive.
- **VIVE Pro & DRM master**: the VIVE requires KWin's DRM master (exclusive kernel-mode lock).
  Private Xwayland (Plan C2) cannot work — the VIVE is exposed via `wp_drm_lease_device_v1`
  only, and monado's compositor list does not include DRM lease acquisition.
  `start_xwayland_99.sh` is kept in tree for reference but not used by default.
- **`pixi run kde-recover`** tries to revive a crashed KDE `:1` Xwayland via
  `gdbus org.kde.KWin reconfigure`; if that fails it prints manual recovery
  steps (TTY + `kwin_wayland --replace`, logout/login, or reboot).

## Known issue: VIVE DP-3 stays disabled after monado exits

**Symptom**: `pixi run monado-detached` starts monado but the VIVE green
light never turns on.  `monado-status` shows `IPC: MISSING`, log ends at
`compositor_check_and_prepare_xdev`, and `/sys/class/drm/card1-DP-3/enabled`
reads `disabled`.

**Cause**: NVIDIA proprietary driver (580.x) does not properly release the
VIVE Pro DisplayPort after monado exits via SIGINT/SIGTERM.  The kernel
keeps DP-3 in "disabled" state, and the next `vkAcquireXlibDisplayEXT` call
hangs indefinitely inside monado's compositor init.

**Workaround**: **Reboot the system.**  The first monado run after a fresh boot
always works (verified FPS 90.0).  Subsequent runs fail until reboot.

## Known issue: monado "buffer overflow detected" crash (VIVE sensor state)

**Symptom**: monado aborts with `*** buffer overflow detected ***: terminated`
(SIGABRT) — either at startup right after `vive_device_create` logs, or
mid-session.  Often preceded by garbage IMU ranges in the log
(`Gyroscope: 177 / Accelerometer: 112`, valid range is 0–4) and
`Firmware version 0`.

**Cause**: two independent problems, see
`mujoco_vive_scripts/monado_buffer_overflow.md` for the full analysis:
1. The VIVE Pro sensor MCU returned garbage reports (bad firmware state).
2. monado's `_print_v2_pulse()` (vive driver) has a real 1-byte stack
   overflow: `sprintf(&data_str[31], "_")` at the last loop iteration.
   **Any 59-byte `VIVE_HEADSET_LIGHTHOUSE_V2_PULSE_REPORT_ID` report kills
   monado deterministically** — this is the real code behind the old
   "keep base stations OFF" rule.  Still unfixed upstream.

**STATUS: FIXED LOCALLY** (2026-08-09).  A patched `monado-service`
(21.0.0+git2905.e26a272c1~dfsg1-2build2) is installed at
`/usr/local/bin/monado-service` and shadows the distro binary via PATH.
Patch: `patches/monado-vive-print-v2-pulse-overflow.patch`.
Source + rebuild instructions: `~/monado-build` (CMake; the embedded
version string must be exactly `GIT-NOTFOUND` to match the distro client
lib — see the `set(GIT_DESC "GIT-NOTFOUND")` override in the local
CMakeLists.txt; `-DGIT_DESC` won't work since CMake treats any value
ending in `-NOTFOUND` as false, and `IPC_IGNORE_VERSION=1` is the
fallback for clients).

**Workaround** (still applies to the *hardware* side): power-cycle the VIVE
(unplug Link Box USB/power for ~10s, re-enumerates the sensor MCU).  Keep
base stations and controllers OFF.

**Check**: `cat /sys/class/drm/card1-DP-3/enabled`.  If `disabled` and you
haven't rebooted since the last successful monado run → you have hit this bug.

**Future fix**: either a newer NVIDIA driver that properly releases
DisplayPort resources, or a kernel patch, or monado calling
`vkReleaseDisplayEXT` before exit.

## Known issue: Xwayland crashes on monado-stop

**Symptom**: `pixi run monado-stop` works (monado exits) but the KDE session
shows "Xwayland has crashed" notification and VSCode may briefly
flash / disconnect.

**Cause**: NOT a race in monado — it is a **use-after-free inside Xwayland**
(`hw/xwayland/xwayland-drm-lease.c`).  monado's teardown releases the VIVE's
DRM lease; Xwayland's `wp_drm_lease_connector_v1.withdrawn` handler frees the
`xwl_output` while it is still referenced by `rrLease->outputs[]`, then the
`wp_drm_lease_v1.finished` handler walks the dangling pointer and SEGVs.
Full evidence chain (apport core + disassembly + source match) and fix
record: `mujoco_vive_scripts/xwayland_crash_analysis.md`.

**STATUS: FIXED LOCALLY** (2026-08-10, re-applied 2026-08-26).  A rebuilt
`xwayland` (23.2.6-1ubuntu0.8, dpkg-installed) backports upstream commits
`f6cd168d` ("Do not remove output on withdraw if leased", the actual fix)
and `b67e0233` (NULL-check hardening).  Full rebuild procedure + incident
record: `mujoco_vive_scripts/xwayland_rebuild.md`; patch at
`patches/xwayland-drm-lease-uaf-fix.patch`; sources + built deb at
`~/xwayland-build`.

**CRITICAL**: the rebuilt package has the SAME version as the distro one,
so `apt upgrade` can silently reinstall the stock binary over it
(happened 2026-08-11; crash recurred 2026-08-26 with identical signature).
After `dpkg -i`, immediately run `sudo apt-mark hold xwayland` and verify
with `apt-mark showhold`.  Verify the installed binary with
`file /usr/bin/Xwayland` (patched BuildID = `2b56b7d2...`, stock =
`d262b443...`).

Ubuntu noble will not ship this backport; on any future xwayland version
upgrade, check whether upstream already includes `f6cd168d`, else rebuild
from `~/xwayland-build` per the doc.  After installing a new Xwayland
binary, restart Xwayland (`pkill -9 -x Xwayland`; KWin auto-restarts it
only after a *crash*, not after a clean exit — use `kwin_wayland
--replace` in the latter case).

**Mitigation in place** (still useful as belt-and-braces):
1. `~/.config/kwinrc`: `XwaylandCrashPolicy=1` (Restart).  KWin
   automatically restarts :1 after a crash.
2. `pixi run monado-stop` now **automatically detects the crash and
   triggers KWin recovery** (DBus `org.kde.KWin.reconfigure`), polling
   up to 15s for :1 to come back.
3. `pixi run kde-recover` can be run manually if auto-recovery doesn't
   catch it.

## KDE Xwayland crash policy

Default behaviour of KDE Plasma when Xwayland crashes:

- `XwaylandCrashPolicy=Stop` (0): stop the whole session — **avoid**.
- `XwaylandCrashPolicy=Restart` (1): restart only Xwayland — **recommended**.
  This is the KDE default, but it can be overridden.

Current setting in `~/.config/kwinrc`:

```ini
[Xwayland]
Scale=1.5
XwaylandCrashPolicy=1   # Restart (KDE default); belt-and-braces against monado crashes
```

With the detached-session approach, KDE's `:1` is still used by monado
directly — the isolation comes from `setsid`, not from a separate X server.

## MuJoCo rendering notes

- `save_stereo_frames.py` sets `MUJOCO_GL=egl` by default for offscreen headless rendering on NVIDIA.
- `mujoco_sbs_glfw.py` uses GLFW via `mujoco.glfw`. It renders to regular windows/monitors — not into the OpenXR compositor. When Monado direct-mode owns the HMD, GLFW windows won't appear on the VIVE.
- The test model at `models/stereo_endoscope_test.xml` has fixed cameras named `endo_left` and `endo_right`.

## Adding new Python modules

Add public entrypoints in `mujoco_vive_scripts/pyproject.toml` `[project.scripts]` and a matching task in the top-level `pixi.toml`.

### Shared infrastructure

All XR entry points reuse:

- `xr_common._NvidiaEGLContextProvider` — headless EGL pbuffer context (libEGL ctypes).
- `xr_common._StdinReader` — non-blocking single-key reader with a clean TTY fallback.
- `xr_common.RuntimeComfortState` + `_handle_input` — comfort control surface.
- `xr_common.add_calibration_args` / `add_comfort_args` — argparse factories for shared CLI flags.

New XR scripts should `from .xr_common import ...` rather than re-implementing the EGL provider.
