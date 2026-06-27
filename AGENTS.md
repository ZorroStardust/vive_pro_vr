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
| Start Monado (Wayland windowed) | `pixi run start-monado` |
| Test hello_xr against Monado | `pixi run test-openxr` |
| OpenXR env check | `pixi run openxr-check` |
| OpenXR pink world (left/right pure color) | `pixi run xr-pink-world` |
| OpenXR MuJoCo stereo (comfort controls) | `pixi run xr-mujoco-opengl` |
| OpenXR crosshair calibration tool | `pixi run xr-crosshair` |
| OpenXR surgical robot stereo preview | `pixi run xr-surgical` |
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
- Monado **must have a PTY** for stdin epoll. Use tmux, **never** bare `monado-service &`.
- IPC socket: `/run/user/$(id -u)/monado_comp_ipc`. Delete stale sockets on failure.
- **Base stations and controllers must be OFF** during display-only testing. When on, Lighthouse pulse reports can overflow Monado's buffer.
- Always check for `1 active app session(s)` in Monado log to confirm client connection succeeded.

## Process management gotchas

- Never `pkill -f hello_xr` — it matches the script filename and kills the script. Use `pkill -x hello_xr`.
- Never `sudo` OpenXR clients — the IPC socket is per-user under `/run/user/$UID/`.

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
