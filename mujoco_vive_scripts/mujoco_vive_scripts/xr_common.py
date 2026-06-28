"""Shared EGL/OpenXR/stdin/comfort utilities for the mujoco_vive_scripts XR tools.

This module is the single source of truth for:
  * raw EGL pbuffer context creation (``_NvidiaEGLContextProvider``)
  * OpenXR extension checks (``check_openxr``, ``_REQUIRED_EXTENSIONS``)
  * pyopenxr opaque-pointer unwrapping helpers
  * non-blocking single-key stdin reader (``_StdinReader``)
  * ``RuntimeComfortState`` dataclass + input handling
  * argparse factories for the shared comfort/calibration CLI surface
  * ``ScreenSink`` / ``OpenXRSink`` — unified sink interface for HMD vs. screen rendering

Nothing here imports from sibling modules, so it is safe to import from
``xr_mujoco_opengl``, ``xr_surgical_robot`` and ``xr_crosshair_calibration``
without introducing cycles.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import select
import sys
import termios
import time
import fcntl
from dataclasses import dataclass


_REQUIRED_EXTENSIONS = (
    "XR_KHR_opengl_enable",
    "XR_MNDX_egl_enable",
)


HINT = """
MuJoCo VR Comfort Controls
────────────────────────────────
 a / d   move whole stereo scene farther / nearer
 e       reset scene comfort shift to zero

 z / x   zoom out / in (wider / narrower FOV)

 p       toggle mono-to-both-eyes diagnostic mode
 o       toggle swap-eyes

 s       save current settings to calibration.toml
 q / esc quit

Recommended workflow:
 1. Try p first. Mono-to-both-eyes should be easiest to fuse.
 2. If mono is comfortable but stereo is tiring, reduce stereo stress with a.
 3. If positive farther shift feels reversed, use --invert-scene-shift or toggle swap-eyes.
 4. Press z/x to adjust FOV until the scene fills the view comfortably.
 5. Press s to save settings for next session.
"""


def _extension_name_to_str(name) -> str:
    if isinstance(name, bytes):
        return name.decode()
    return str(name)


def _xr_field_type(struct_cls, field_name: str):
    for item in struct_cls._fields_:
        if item[0] == field_name:
            return item[1]
    raise RuntimeError(f"Field not found: {struct_cls.__name__}.{field_name}")


def _handle_address(handle) -> int:
    if handle is None:
        return 0

    if isinstance(handle, int):
        return handle

    if isinstance(handle, ctypes.c_void_p):
        return int(handle.value or 0)

    try:
        return int(ctypes.cast(handle, ctypes.c_void_p).value or 0)
    except Exception:
        pass

    return int(handle)


def _cast_xr_egl_handle(handle, field_name: str):
    addr = _handle_address(handle)
    if addr == 0:
        raise RuntimeError(f"EGL handle for {field_name!r} is NULL")

    import xr

    field_type = _xr_field_type(xr.GraphicsBindingEGLMNDX, field_name)

    try:
        if issubclass(field_type, ctypes._Pointer):
            return ctypes.cast(ctypes.c_void_p(addr), field_type)
    except TypeError:
        pass

    try:
        return field_type(addr)
    except TypeError:
        return ctypes.cast(ctypes.c_void_p(addr), field_type)


_gl_module = None


def _get_gl():
    """Lazy + cached ``OpenGL.GL`` import.

    Resolved once on first call so the per-frame ``_drain_gl_errors`` call
    avoids repeated ``sys.modules`` lookups.
    """
    global _gl_module
    if _gl_module is None:
        from OpenGL import GL as _gl_module
    return _gl_module


_GL_ERROR_PRINT_COUNTS: dict[tuple[str, tuple[int, ...]], int] = {}


def _drain_gl_errors(label: str = "", print_limit: int = 2) -> None:
    """Drain pending OpenGL errors.

    MuJoCo may leave a sticky GL error after C-side rendering. PyOpenGL checks
    errors after later Python GL calls, so we drain errors here to prevent
    pyopenxr from seeing unrelated stale errors.

    print_limit controls how many times each unique error/label pair is printed.
    Set print_limit=0 for silent draining.
    """
    GL = _get_gl()

    errors: list[int] = []
    while True:
        err = GL.glGetError()
        if err == GL.GL_NO_ERROR:
            break
        errors.append(int(err))

    if not errors:
        return

    key = (label, tuple(errors))
    count = _GL_ERROR_PRINT_COUNTS.get(key, 0)
    _GL_ERROR_PRINT_COUNTS[key] = count + 1

    if count < print_limit:
        print(f"[WARN] Drained OpenGL errors after {label}: {[hex(e) for e in errors]}")
    elif count == print_limit:
        print(f"[WARN] Further identical OpenGL errors after {label} will be suppressed.")


class _NvidiaEGLContextProvider:
    """Headless EGL pbuffer context using raw libEGL ctypes calls.

    This avoids PyOpenGL opaque-pointer conversion issues around
    eglQueryDevicesEXT / eglGetPlatformDisplayEXT.
    """

    EGL_FALSE = 0
    EGL_TRUE = 1

    EGL_DEFAULT_DISPLAY = 0
    EGL_NO_DISPLAY = 0
    EGL_NO_CONTEXT = 0
    EGL_NO_SURFACE = 0

    EGL_NONE = 0x3038
    EGL_RED_SIZE = 0x3024
    EGL_GREEN_SIZE = 0x3023
    EGL_BLUE_SIZE = 0x3022
    EGL_ALPHA_SIZE = 0x3021
    EGL_DEPTH_SIZE = 0x3025
    EGL_SURFACE_TYPE = 0x3033
    EGL_RENDERABLE_TYPE = 0x3040
    EGL_PBUFFER_BIT = 0x0001
    EGL_OPENGL_BIT = 0x0008

    EGL_WIDTH = 0x3057
    EGL_HEIGHT = 0x3056

    EGL_OPENGL_API = 0x30A2

    EGL_CONTEXT_MAJOR_VERSION = 0x3098
    EGL_CONTEXT_MINOR_VERSION = 0x30FB
    EGL_CONTEXT_OPENGL_PROFILE_MASK = 0x30FD
    EGL_CONTEXT_OPENGL_COMPATIBILITY_PROFILE_BIT = 0x00000002

    EGL_EXTENSIONS = 0x3055
    EGL_VENDOR = 0x3053
    EGL_VERSION = 0x3054

    EGL_PLATFORM_DEVICE_EXT = 0x313F

    _PBUFFER_W = 512
    _PBUFFER_H = 512
    _MAX_DEVICES = 16
    _MAX_CONFIGS = 32

    def __init__(self):
        import ctypes.util

        lib_name = ctypes.util.find_library("EGL")
        if not lib_name:
            raise RuntimeError("Could not find libEGL.so")

        self._libegl = ctypes.CDLL(lib_name)

        # Basic EGL functions.
        self._libegl.eglGetError.restype = ctypes.c_uint32

        self._libegl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
        self._libegl.eglGetProcAddress.restype = ctypes.c_void_p

        self._libegl.eglGetDisplay.argtypes = [ctypes.c_void_p]
        self._libegl.eglGetDisplay.restype = ctypes.c_void_p

        self._libegl.eglInitialize.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self._libegl.eglInitialize.restype = ctypes.c_uint32

        self._libegl.eglChooseConfig.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        self._libegl.eglChooseConfig.restype = ctypes.c_uint32

        self._libegl.eglBindAPI.argtypes = [ctypes.c_uint32]
        self._libegl.eglBindAPI.restype = ctypes.c_uint32

        self._libegl.eglCreatePbufferSurface.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        self._libegl.eglCreatePbufferSurface.restype = ctypes.c_void_p

        self._libegl.eglCreateContext.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        self._libegl.eglCreateContext.restype = ctypes.c_void_p

        self._libegl.eglMakeCurrent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._libegl.eglMakeCurrent.restype = ctypes.c_uint32

        self._libegl.eglDestroySurface.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._libegl.eglDestroySurface.restype = ctypes.c_uint32

        self._libegl.eglDestroyContext.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._libegl.eglDestroyContext.restype = ctypes.c_uint32

        self._libegl.eglQueryString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self._libegl.eglQueryString.restype = ctypes.c_char_p

        # Extension function pointers.
        PFNEGLQUERYDEVICESEXTPROC = ctypes.CFUNCTYPE(
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int),
        )

        PFNEGLGETPLATFORMDISPLAYEXTPROC = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        )

        query_devices_ptr = self._libegl.eglGetProcAddress(
            b"eglQueryDevicesEXT"
        )
        get_platform_display_ptr = self._libegl.eglGetProcAddress(
            b"eglGetPlatformDisplayEXT"
        )

        self._eglQueryDevicesEXT = (
            PFNEGLQUERYDEVICESEXTPROC(query_devices_ptr)
            if query_devices_ptr
            else None
        )
        self._eglGetPlatformDisplayEXT = (
            PFNEGLGETPLATFORMDISPLAYEXTPROC(get_platform_display_ptr)
            if get_platform_display_ptr
            else None
        )

        self._display = None
        self._surface = None
        self._context = None
        self._config = None

        last_error = None

        # Preferred path: EGL_EXT_platform_device.
        if self._eglQueryDevicesEXT and self._eglGetPlatformDisplayEXT:
            max_devices = self._MAX_DEVICES
            devices = (ctypes.c_void_p * max_devices)()
            num_devices = ctypes.c_int(0)

            ok = self._eglQueryDevicesEXT(
                max_devices,
                devices,
                ctypes.byref(num_devices),
            )

            print(
                f"[DEBUG] raw eglQueryDevicesEXT ok={ok}, "
                f"num_devices={num_devices.value}"
            )

            for i in range(num_devices.value):
                device = devices[i]
                print(f"[DEBUG] Trying raw EGL device {i}: 0x{int(device):x}")

                dpy = self._eglGetPlatformDisplayEXT(
                    self.EGL_PLATFORM_DEVICE_EXT,
                    device,
                    None,
                )

                if not dpy:
                    last_error = (
                        f"raw eglGetPlatformDisplayEXT returned NULL "
                        f"for device {i}, eglGetError=0x{self._egl_error():x}"
                    )
                    print(f"[DEBUG] {last_error}")
                    continue

                if self._try_init_display(dpy, f"platform_device[{i}]"):
                    break
        else:
            last_error = "eglQueryDevicesEXT or eglGetPlatformDisplayEXT not available"

        # Fallback path: EGL_DEFAULT_DISPLAY.
        # This is still EGL, not GLX. It may work on NVIDIA even when
        # platform_device path fails through the current Python stack.
        if self._display is None:
            print("[DEBUG] Falling back to eglGetDisplay(EGL_DEFAULT_DISPLAY)")
            dpy = self._libegl.eglGetDisplay(
                ctypes.c_void_p(self.EGL_DEFAULT_DISPLAY)
            )
            if dpy:
                self._try_init_display(dpy, "EGL_DEFAULT_DISPLAY")
            else:
                last_error = (
                    f"eglGetDisplay(EGL_DEFAULT_DISPLAY) returned NULL, "
                    f"eglGetError=0x{self._egl_error():x}"
                )

        if self._display is None:
            raise RuntimeError(
                "No usable EGL display found. "
                f"Last error: {last_error}"
            )

        # These public fields are consumed by pyopenxr's EGLGraphicsBinding.
        self.display = _cast_xr_egl_handle(self._display, "display")
        self.context = _cast_xr_egl_handle(self._context, "context")
        self.config = _cast_xr_egl_handle(self._config, "config")

        GL = _get_gl()

        print("[INFO] Headless EGL context ready.")
        print(f"[INFO] GL_VENDOR   = {GL.glGetString(GL.GL_VENDOR)}")
        print(f"[INFO] GL_RENDERER = {GL.glGetString(GL.GL_RENDERER)}")
        print(f"[INFO] GL_VERSION  = {GL.glGetString(GL.GL_VERSION)}")
        try:
            profile = GL.glGetIntegerv(GL.GL_CONTEXT_PROFILE_MASK)
            profile = int(profile)
            print(f"[INFO] GL_CONTEXT_PROFILE_MASK = 0x{profile:x}")

            if profile & GL.GL_CONTEXT_CORE_PROFILE_BIT:
                print("[WARN] OpenGL context is CORE profile.")

            if profile & GL.GL_CONTEXT_COMPATIBILITY_PROFILE_BIT:
                print("[INFO] OpenGL context is COMPATIBILITY profile.")
        except Exception as exc:
            print(f"[WARN] Could not query GL profile mask: {exc}")

    def _egl_error(self) -> int:
        try:
            return int(self._libegl.eglGetError())
        except Exception:
            return 0

    def _egl_query_string(self, dpy, name) -> str:
        value = self._libegl.eglQueryString(dpy, name)
        if not value:
            return ""
        return value.decode(errors="replace")

    def _try_init_display(self, dpy, label: str) -> bool:
        major = ctypes.c_int(0)
        minor = ctypes.c_int(0)

        ok = self._libegl.eglInitialize(
            dpy,
            ctypes.byref(major),
            ctypes.byref(minor),
        )

        if not ok:
            print(
                f"[DEBUG] eglInitialize failed for {label}, "
                f"eglGetError=0x{self._egl_error():x}"
            )
            return False

        print(
            f"[DEBUG] eglInitialize OK for {label}: "
            f"EGL {major.value}.{minor.value}"
        )
        print(f"[DEBUG] EGL_VENDOR  = {self._egl_query_string(dpy, self.EGL_VENDOR)}")
        print(f"[DEBUG] EGL_VERSION = {self._egl_query_string(dpy, self.EGL_VERSION)}")

        cfg_attrs = (ctypes.c_int * 17)(
            self.EGL_RED_SIZE,
            8,
            self.EGL_GREEN_SIZE,
            8,
            self.EGL_BLUE_SIZE,
            8,
            self.EGL_ALPHA_SIZE,
            8,
            self.EGL_DEPTH_SIZE,
            24,
            self.EGL_RENDERABLE_TYPE,
            self.EGL_OPENGL_BIT,
            self.EGL_SURFACE_TYPE,
            self.EGL_PBUFFER_BIT,
            self.EGL_NONE,
        )

        cfgs = (ctypes.c_void_p * self._MAX_CONFIGS)()
        num_cfgs = ctypes.c_int(0)

        ok = self._libegl.eglChooseConfig(
            dpy,
            cfg_attrs,
            cfgs,
            len(cfgs),
            ctypes.byref(num_cfgs),
        )

        if not ok or num_cfgs.value <= 0:
            print(
                f"[DEBUG] eglChooseConfig failed for {label}, "
                f"num_cfgs={num_cfgs.value}, eglGetError=0x{self._egl_error():x}"
            )
            return False

        cfg = cfgs[0]
        if not cfg:
            print(f"[DEBUG] eglChooseConfig returned NULL config for {label}")
            return False

        pb_attrs = (ctypes.c_int * 5)(
            self.EGL_WIDTH,
            self._PBUFFER_W,
            self.EGL_HEIGHT,
            self._PBUFFER_H,
            self.EGL_NONE,
        )

        surface = self._libegl.eglCreatePbufferSurface(
            dpy,
            cfg,
            pb_attrs,
        )

        if not surface:
            print(
                f"[DEBUG] eglCreatePbufferSurface failed for {label}, "
                f"eglGetError=0x{self._egl_error():x}"
            )
            return False

        ok = self._libegl.eglBindAPI(self.EGL_OPENGL_API)
        if not ok:
            print(
                f"[DEBUG] eglBindAPI(EGL_OPENGL_API) failed for {label}, "
                f"eglGetError=0x{self._egl_error():x}"
            )
            return False

        # First try OpenGL 3.3 compatibility profile.
        ctx_attrs = (ctypes.c_int * 7)(
            self.EGL_CONTEXT_OPENGL_PROFILE_MASK,
            self.EGL_CONTEXT_OPENGL_COMPATIBILITY_PROFILE_BIT,
            self.EGL_CONTEXT_MAJOR_VERSION,
            3,
            self.EGL_CONTEXT_MINOR_VERSION,
            3,
            self.EGL_NONE,
        )

        ctx = self._libegl.eglCreateContext(
            dpy,
            cfg,
            ctypes.c_void_p(self.EGL_NO_CONTEXT),
            ctx_attrs,
        )

        if not ctx:
            print(
                f"[DEBUG] Compatibility context failed for {label}, "
                f"eglGetError=0x{self._egl_error():x}; trying default context"
            )
            fallback_attrs = (ctypes.c_int * 1)(self.EGL_NONE)
            ctx = self._libegl.eglCreateContext(
                dpy,
                cfg,
                ctypes.c_void_p(self.EGL_NO_CONTEXT),
                fallback_attrs,
            )

        if not ctx:
            print(
                f"[DEBUG] eglCreateContext failed for {label}, "
                f"eglGetError=0x{self._egl_error():x}"
            )
            return False

        ok = self._libegl.eglMakeCurrent(
            dpy,
            surface,
            surface,
            ctx,
        )

        if not ok:
            print(
                f"[DEBUG] eglMakeCurrent failed for {label}, "
                f"eglGetError=0x{self._egl_error():x}"
            )
            return False

        self._display = dpy
        self._surface = surface
        self._context = ctx
        self._config = cfg

        print(f"[INFO] Using EGL display route: {label}")
        return True

    def make_current(self) -> None:
        self._libegl.eglMakeCurrent(
            self._display,
            self._surface,
            self._surface,
            self._context,
        )

    def done_current(self) -> None:
        self._libegl.eglMakeCurrent(
            self._display,
            ctypes.c_void_p(self.EGL_NO_SURFACE),
            ctypes.c_void_p(self.EGL_NO_SURFACE),
            ctypes.c_void_p(self.EGL_NO_CONTEXT),
        )

    def destroy(self) -> None:
        if self._display is None:
            return

        try:
            self.done_current()
        except Exception:
            pass

        if self._surface is not None:
            try:
                self._libegl.eglDestroySurface(
                    self._display,
                    self._surface,
                )
            except Exception:
                pass
            self._surface = None

        if self._context is not None:
            try:
                self._libegl.eglDestroyContext(
                    self._display,
                    self._context,
                )
            except Exception:
                pass
            self._context = None


class _StdinReader:
    """Non-blocking single-key reader.

    If stdin is not a TTY, this becomes a no-op reader so the renderer can still
    run in a non-interactive launcher.
    """

    def __init__(self):
        self._enabled = False
        self._fd: int | None = None
        self._old = None
        self._old_flags = None

        try:
            if not sys.stdin.isatty():
                return

            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            self._old_flags = fcntl.fcntl(self._fd, fcntl.F_GETFL)

            new = termios.tcgetattr(self._fd)
            new[3] = new[3] & ~(termios.ECHO | termios.ICANON)

            termios.tcsetattr(self._fd, termios.TCSANOW, new)
            fcntl.fcntl(self._fd, fcntl.F_SETFL, self._old_flags | os.O_NONBLOCK)
            self._enabled = True
        except Exception as exc:
            print(f"[WARN] Keyboard control disabled: {exc}")
            self._enabled = False

    def restore(self) -> None:
        if not self._enabled or self._fd is None:
            return

        try:
            if self._old is not None:
                termios.tcsetattr(self._fd, termios.TCSANOW, self._old)
            if self._old_flags is not None:
                fcntl.fcntl(self._fd, fcntl.F_SETFL, self._old_flags)
        except Exception:
            pass

    def read_key(self) -> str | None:
        if not self._enabled or self._fd is None:
            return None

        if select.select([sys.stdin], [], [], 0)[0]:
            try:
                data = os.read(self._fd, 16)
                return data.decode(errors="replace")
            except (OSError, BlockingIOError):
                pass

        return None


@dataclass
class RuntimeComfortState:
    swap_eyes: bool = False
    mono_to_both_eyes: bool = False

    # mono_fast: when True AND mono_to_both_eyes is True, the renderer builds
    # the MuJoCo scene once per frame into an offscreen FBO and blits that
    # texture to both eye viewports, halving ``mjv_updateScene`` / ``mjr_render``
    # cost.  Currently honoured by ``MujocoStereoRenderer.render_eye``.
    mono_fast: bool = False

    # Positive scene_farther_px shifts left-eye image left and right-eye image right.
    # This usually reduces crossed disparity and makes the scene feel farther.
    scene_farther_px: float = 0.0
    scene_shift_step_px: float = 2.0
    max_abs_scene_shift_px: float = 80.0
    invert_scene_shift: bool = False

    # zoom > 1 means wider FOV (see more), modifying cam_fovy = original / zoom
    zoom: float = 1.0
    zoom_step: float = 0.1

    def clamp(self) -> None:
        self.scene_farther_px = max(
            -self.max_abs_scene_shift_px,
            min(self.max_abs_scene_shift_px, self.scene_farther_px),
        )

    def move_farther(self) -> None:
        self.scene_farther_px += self.scene_shift_step_px
        self.clamp()

    def move_nearer(self) -> None:
        self.scene_farther_px -= self.scene_shift_step_px
        self.clamp()

    def reset_shift(self) -> None:
        self.scene_farther_px = 0.0

    def zoom_out(self) -> None:
        self.zoom = min(self.zoom + self.zoom_step, 10.0)

    def zoom_in(self) -> None:
        self.zoom = max(self.zoom - self.zoom_step, 0.1)


def _print_comfort_state(prefix: str, state: RuntimeComfortState, end: str = "\n") -> None:
    print(
        f"{prefix} "
        f"ZOOM={state.zoom:.2f} "
        f"SCENE_FARTHER={state.scene_farther_px:+.1f}px "
        f"MONO={'on' if state.mono_to_both_eyes else 'off'} "
        f"SWAP={'on' if state.swap_eyes else 'off'}",
        end=end,
        flush=True,
    )


def _handle_input(reader: _StdinReader, state: RuntimeComfortState,
                  unhandled_cb=None) -> bool:
    key = reader.read_key()
    if key is None:
        return True

    ch = key[-1] if key else ""
    changed = False

    if ch in ("\x1b", "q"):
        print("\n[INFO] Quit requested.")
        return False

    if ch == "a":
        state.move_farther()
        changed = True
    elif ch == "d":
        state.move_nearer()
        changed = True
    elif ch == "e":
        state.reset_shift()
        changed = True
    elif ch == "p":
        state.mono_to_both_eyes = not state.mono_to_both_eyes
        changed = True
    elif ch == "o":
        state.swap_eyes = not state.swap_eyes
        changed = True
    elif ch == "z":
        state.zoom_out()
        changed = True
    elif ch == "x":
        state.zoom_in()
        changed = True
    elif ch == "s":
        from .config_util import (
            Calibration,
            ComfortConfig,
            save_full_config,
            load_calibration,
            _default_config_path,
        )

        cfg_path = _default_config_path()
        cal = load_calibration(cfg_path) or Calibration()
        comfort = ComfortConfig(
            mono_to_both_eyes=state.mono_to_both_eyes,
            swap_eyes=state.swap_eyes,
            scene_farther_px=state.scene_farther_px,
            scene_shift_step_px=state.scene_shift_step_px,
            max_abs_scene_shift_px=state.max_abs_scene_shift_px,
            invert_scene_shift=state.invert_scene_shift,
            zoom=state.zoom,
            zoom_step=state.zoom_step,
        )
        save_full_config(cal, comfort, cfg_path)
        print(f"\r[SAVED] {cfg_path}", flush=True)
    elif unhandled_cb is not None and not changed:
        if not unhandled_cb(ch):
            return False

    if changed:
        _print_comfort_state("\r[COMFORT]", state, end="")

    return True


def check_openxr() -> None:
    import xr

    names = {
        _extension_name_to_str(ext.extension_name)
        for ext in xr.enumerate_instance_extension_properties()
    }

    for ext in _REQUIRED_EXTENSIONS:
        if ext not in names:
            raise RuntimeError(f"{ext} is not available")

    print("[INFO] Required OpenXR extensions available:")
    for ext in _REQUIRED_EXTENSIONS:
        print(f"       {ext}")


# ---- argparse factories ---------------------------------------------------

def add_calibration_args(parser: argparse.ArgumentParser, calib) -> None:
    """Add the four ``--calib-*-{x,y}`` pixel-offset flags.

    Parameters
    ----------
    parser:
        Target ``argparse.ArgumentParser``.
    calib:
        A ``Calibration`` instance (or ``None``) whose fields are used as
        defaults.  When ``None``, all defaults are 0.
    """
    parser.add_argument(
        "--calib-left-x",
        type=int,
        default=calib.left_x if calib else 0,
        help="Left-eye horizontal calibration offset in pixels.",
    )
    parser.add_argument(
        "--calib-left-y",
        type=int,
        default=calib.left_y if calib else 0,
        help="Left-eye vertical calibration offset in pixels.",
    )
    parser.add_argument(
        "--calib-right-x",
        type=int,
        default=calib.right_x if calib else 0,
        help="Right-eye horizontal calibration offset in pixels.",
    )
    parser.add_argument(
        "--calib-right-y",
        type=int,
        default=calib.right_y if calib else 0,
        help="Right-eye vertical calibration offset in pixels.",
    )


def add_comfort_args(parser: argparse.ArgumentParser, comfort) -> None:
    """Add the comfort / depth / zoom / clear-color CLI flags."""
    parser.add_argument(
        "--swap-eyes", action="store_true",
        default=comfort.swap_eyes if comfort else False,
    )
    parser.add_argument(
        "--mono-to-both-eyes",
        action="store_true",
        default=comfort.mono_to_both_eyes if comfort else False,
        help="Render the left MuJoCo camera to both eyes. Easiest to fuse; useful for diagnosis.",
    )
    parser.add_argument(
        "--scene-farther-px",
        type=float,
        default=comfort.scene_farther_px if comfort else 0.0,
        help=(
            "Shift stereo images outward. Positive usually makes the whole MuJoCo "
            "scene feel farther and less crossed."
        ),
    )
    parser.add_argument(
        "--scene-shift-step-px",
        type=float,
        default=comfort.scene_shift_step_px if comfort else 2.0,
        help="Runtime keyboard adjustment step for scene farther/nearer shift.",
    )
    parser.add_argument(
        "--max-scene-shift-px",
        type=float,
        default=comfort.max_abs_scene_shift_px if comfort else 80.0,
        help="Maximum absolute scene shift in pixels.",
    )
    parser.add_argument(
        "--invert-scene-shift",
        action="store_true",
        default=comfort.invert_scene_shift if comfort else False,
        help="Invert scene farther/nearer shift direction if it feels reversed.",
    )
    parser.add_argument(
        "--zoom",
        type=float,
        default=comfort.zoom if comfort else 1.0,
        help="FOV zoom factor. >1 = wider FOV (see more). Modifies cam_fovy = original/zoom.",
    )
    parser.add_argument(
        "--zoom-step",
        type=float,
        default=comfort.zoom_step if comfort else 0.1,
        help="Keyboard zoom adjustment step.",
    )
    parser.add_argument(
        "--clear-rgb",
        type=float,
        nargs=3,
        metavar=("R", "G", "B"),
        default=comfort.to_rgb_tuple() if comfort else (0.02, 0.02, 0.02),
        help="Background clear color, each value 0..1. Lower contrast often feels easier.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save calibration and comfort settings to config file on exit.",
    )


# ---- Screen / XR sink abstraction ---------------------------------------


@dataclass
class _FrameState:
    """Thin substitute for xr.FrameState in screen mode."""
    pass


@dataclass
class _ViewState:
    """Thin substitute for xr.ViewState in screen mode."""
    pass


def add_screen_args(parser: argparse.ArgumentParser) -> None:
    """Add ``--screen``, ``--sbs-layout`` and related screen-mode flags.

    Call this in every XR entry point alongside
    :py:func:`add_calibration_args` and :py:func:`add_comfort_args`.
    """
    parser.add_argument(
        "--screen",
        action="store_true",
        help="Render to a desktop window instead of the VR HMD (no Monado or OpenXR required).",
    )
    parser.add_argument(
        "--screen-width",
        type=int,
        default=1440,
        help="Window width in pixels (screen mode only).",
    )
    parser.add_argument(
        "--screen-height",
        type=int,
        default=800,
        help="Window height in pixels (screen mode only).",
    )
    parser.add_argument(
        "--screen-fullscreen",
        action="store_true",
        help="Use fullscreen window (screen mode only).",
    )
    parser.add_argument(
        "--screen-monitor",
        type=int,
        default=0,
        help="Monitor index for fullscreen (screen mode only).",
    )
    parser.add_argument(
        "--screen-fps",
        type=float,
        default=60.0,
        help="Target FPS for screen mode.",
    )
    parser.add_argument(
        "--screen-title",
        default="MuJoCo Stereo — Screen",
        help="Window title (screen mode only).",
    )
    parser.add_argument(
        "--sbs-layout",
        choices=("vertical", "horizontal"),
        default="horizontal",
        help="Side-by-side layout: horizontal (side-by-side) or vertical (stacked). Default: horizontal.",
    )


class ScreenSink:
    """GLFW window sink that mimics the OpenXR view/frame loop.

    Used as a drop-in replacement for ``xr.utils.gl.ContextObject`` in
    screen-debug mode.  Creates a desktop window, splits it into two
    eye viewports (left/right), and drives the render loop via ``glfw``.
    """

    def __init__(
        self,
        width: int = 1440,
        height: int = 800,
        fullscreen: bool = False,
        monitor: int = 0,
        fps: float = 60.0,
        layout: str = "horizontal",
        title: str = "MuJoCo Stereo — Screen",
    ):
        self._width = width
        self._height = height
        self._fullscreen = fullscreen
        self._monitor = monitor
        self._fps = fps
        self._layout = layout
        self._title = title
        self._window = None

    def __enter__(self):
        import glfw as _glfw

        if not _glfw.init():
            raise RuntimeError("GLFW init failed for screen sink")

        os.environ["PYOPENGL_PLATFORM"] = "egl"
        os.environ["MUJOCO_GL"] = "egl"
        global _gl_module
        _gl_module = None

        _glfw.window_hint(_glfw.CLIENT_API, _glfw.OPENGL_API)
        _glfw.window_hint(_glfw.CONTEXT_VERSION_MAJOR, 3)
        _glfw.window_hint(_glfw.CONTEXT_VERSION_MINOR, 3)
        _glfw.window_hint(_glfw.OPENGL_PROFILE, _glfw.OPENGL_COMPAT_PROFILE)

        if self._fullscreen:
            monitors = _glfw.get_monitors()
            if self._monitor >= len(monitors):
                _glfw.terminate()
                raise RuntimeError(
                    f"Monitor {self._monitor} not found "
                    f"(available: 0..{len(monitors) - 1})"
                )
            mon = monitors[self._monitor]
            mode = _glfw.get_video_mode(mon)
            self._window = _glfw.create_window(
                mode.size.width, mode.size.height,
                self._title, mon, None,
            )
        else:
            self._window = _glfw.create_window(
                self._width, self._height,
                self._title, None, None,
            )

        if not self._window:
            _glfw.terminate()
            raise RuntimeError("GLFW create_window failed for screen sink")

        _glfw.make_context_current(self._window)
        _glfw.swap_interval(1)

        GL = _get_gl()
        while GL.glGetError() != GL.GL_NO_ERROR:
            pass

        self._glfw = _glfw
        return self

    def __exit__(self, *exc_info):
        if self._window is not None:
            self._glfw.destroy_window(self._window)
            self._window = None
        self._glfw.terminate()
        return False

    def make_current(self) -> None:
        self._glfw.make_context_current(self._window)

    def frame_loop(self):
        while not self._glfw.window_should_close(self._window):
            t0 = time.perf_counter()
            yield _FrameState()
            self._glfw.swap_buffers(self._window)
            self._glfw.poll_events()
            elapsed = time.perf_counter() - t0
            sleep_s = max(0.0, 1.0 / self._fps - elapsed)
            if sleep_s > 0:
                time.sleep(sleep_s)

    def view_loop(self, frame_state):
        fbw, fbh = self._glfw.get_framebuffer_size(self._window)
        GL = _get_gl()

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glViewport(0, 0, fbw, fbh)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)

        if self._layout == "vertical":
            half = fbh // 2
            views = [
                (0, half, fbw, fbh - half),
                (0, 0, fbw, half),
            ]
        else:
            half = fbw // 2
            views = [
                (0, 0, half, fbh),
                (half, 0, fbw - half, fbh),
            ]

        for _view_index, vp in enumerate(views):
            x, y, w, h = vp
            GL.glViewport(x, y, w, h)
            yield _ViewState()


class OpenXRSink:
    """OpenXR sink wrapping the existing EGL + ContextObject path.

    Provides the same ``frame_loop()`` / ``view_loop()`` API as
    :py:class:`ScreenSink` so callers can switch between HMD and screen
    modes transparently.
    """

    def __init__(self):
        os.environ["PYOPENGL_PLATFORM"] = "egl"
        os.environ["MUJOCO_GL"] = "egl"

        global _gl_module
        _gl_module = None

        check_openxr()
        self._provider = _NvidiaEGLContextProvider()

        import xr

        from xr.utils.gl import ContextObject

        self._ctx = ContextObject(
            context_provider=self._provider,
            instance_create_info=xr.InstanceCreateInfo(
                enabled_extension_names=list(_REQUIRED_EXTENSIONS),
            ),
        )

    def __enter__(self):
        self._ctx.__enter__()
        return self

    def __exit__(self, *exc_info):
        result = self._ctx.__exit__(*exc_info)
        self._provider.destroy()
        return result

    def make_current(self) -> None:
        self._provider.make_current()

    def frame_loop(self):
        return self._ctx.frame_loop()

    def view_loop(self, frame_state):
        return self._ctx.view_loop(frame_state)


def make_sink(args) -> ScreenSink | OpenXRSink:
    """Create the appropriate stereo sink based on CLI args.

    When ``args.screen`` is true returns a :py:class:`ScreenSink`;
    otherwise returns an :py:class:`OpenXRSink`.
    """
    if args.screen:
        return ScreenSink(
            width=args.screen_width,
            height=args.screen_height,
            fullscreen=args.screen_fullscreen,
            monitor=args.screen_monitor,
            fps=args.screen_fps,
            layout=args.sbs_layout,
            title=args.screen_title,
        )
    return OpenXRSink()
