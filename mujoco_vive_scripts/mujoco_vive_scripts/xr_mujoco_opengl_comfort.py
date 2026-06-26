from __future__ import annotations

import argparse
import ctypes
import math
import os
import select
import sys
import termios
import fcntl
import time
from dataclasses import dataclass
from pathlib import Path

# Must be set before importing PyOpenGL.
# Do not import OpenGL.GL at module top.
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

# We are using our own EGL context provider.
os.environ.setdefault("MUJOCO_GL", "egl")

import mujoco
import xr


_EGL_PLATFORM_DEVICE_EXT = 0x313F

_REQUIRED_EXTENSIONS = (
    "XR_KHR_opengl_enable",
    "XR_MNDX_egl_enable",
)


HINT = """
MuJoCo VR Comfort Controls
────────────────────────────────
 a / d   move whole stereo scene farther / nearer
 e       reset scene comfort shift to zero

 p       toggle mono-to-both-eyes diagnostic mode
 o       toggle swap-eyes

 s       save current settings to calibration.toml
 q / esc quit

Recommended workflow:
 1. Try p first. Mono-to-both-eyes should be easiest to fuse.
 2. If mono is comfortable but stereo is tiring, reduce stereo stress with a.
 3. If positive farther shift feels reversed, use --invert-scene-shift or toggle swap-eyes.
 4. Press s to save settings for next session.
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


_GL_ERROR_PRINT_COUNTS: dict[tuple[str, tuple[int, ...]], int] = {}


def _drain_gl_errors(label: str = "", print_limit: int = 2) -> None:
    """Drain pending OpenGL errors.

    MuJoCo may leave a sticky GL error after C-side rendering. PyOpenGL checks
    errors after later Python GL calls, so we drain errors here to prevent
    pyopenxr from seeing unrelated stale errors.

    print_limit controls how many times each unique error/label pair is printed.
    Set print_limit=0 for silent draining.
    """
    from OpenGL import GL

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
            max_devices = 16
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

        from OpenGL import GL

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

        cfgs = (ctypes.c_void_p * 32)()
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
            512,
            self.EGL_HEIGHT,
            512,
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

    # Positive scene_farther_px shifts left-eye image left and right-eye image right.
    # This usually reduces crossed disparity and makes the scene feel farther.
    scene_farther_px: float = 0.0
    scene_shift_step_px: float = 2.0
    max_abs_scene_shift_px: float = 80.0
    invert_scene_shift: bool = False

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


def _print_comfort_state(prefix: str, state: RuntimeComfortState, end: str = "\n") -> None:
    print(
        f"{prefix} "
        f"SCENE_FARTHER={state.scene_farther_px:+.1f}px "
        f"MONO={'on' if state.mono_to_both_eyes else 'off'} "
        f"SWAP={'on' if state.swap_eyes else 'off'}",
        end=end,
        flush=True,
    )


def _handle_input(reader: _StdinReader, state: RuntimeComfortState) -> bool:
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
    elif ch == "s":
        from .config_util import Calibration, ComfortConfig, save_full_config, load_calibration, _default_config_path

        cfg_path = _default_config_path()
        cal = load_calibration(cfg_path) or Calibration()
        comfort = ComfortConfig(
            mono_to_both_eyes=state.mono_to_both_eyes,
            swap_eyes=state.swap_eyes,
            scene_farther_px=state.scene_farther_px,
            scene_shift_step_px=state.scene_shift_step_px,
            max_abs_scene_shift_px=state.max_abs_scene_shift_px,
            invert_scene_shift=state.invert_scene_shift,
        )
        save_full_config(cal, comfort, cfg_path)
        print(f"\r[SAVED] {cfg_path}", flush=True)

    if changed:
        _print_comfort_state("\r[COMFORT]", state, end="")

    return True


@dataclass
class MujocoStereoRenderer:
    model: mujoco.MjModel
    data: mujoco.MjData
    left_id: int
    right_id: int
    option: mujoco.MjvOption
    scene: mujoco.MjvScene
    context: mujoco.MjrContext
    camera: mujoco.MjvCamera
    spin_qposadr: int | None
    calib_left_x: int = 0
    calib_left_y: int = 0
    calib_right_x: int = 0
    calib_right_y: int = 0

    # Rendering comfort parameters.
    clear_r: float = 0.02
    clear_g: float = 0.02
    clear_b: float = 0.02

    @classmethod
    def create(
        cls,
        model_path: str,
        left_camera: str,
        right_camera: str,
        max_geom: int,
        calib_left_x: int = 0,
        calib_left_y: int = 0,
        calib_right_x: int = 0,
        calib_right_y: int = 0,
        clear_rgb: tuple[float, float, float] = (0.02, 0.02, 0.02),
    ):
        if not Path(model_path).exists():
            raise FileNotFoundError(model_path)

        model = mujoco.MjModel.from_xml_path(model_path)
        data = mujoco.MjData(model)

        left_id = mujoco.mj_name2id(
            model,
            mujoco.mjtObj.mjOBJ_CAMERA,
            left_camera,
        )
        right_id = mujoco.mj_name2id(
            model,
            mujoco.mjtObj.mjOBJ_CAMERA,
            right_camera,
        )

        if left_id < 0:
            raise RuntimeError(f"Camera not found: {left_camera}")
        if right_id < 0:
            raise RuntimeError(f"Camera not found: {right_camera}")

        option = mujoco.MjvOption()
        scene = mujoco.MjvScene(model, maxgeom=max_geom)

        _drain_gl_errors("before mujoco.MjrContext")
        context = mujoco.MjrContext(
            model,
            mujoco.mjtFontScale.mjFONTSCALE_150,
        )
        _drain_gl_errors("after mujoco.MjrContext")

        camera = mujoco.MjvCamera()

        jid = mujoco.mj_name2id(
            model,
            mujoco.mjtObj.mjOBJ_JOINT,
            "spin",
        )
        spin_qposadr = int(model.jnt_qposadr[jid]) if jid >= 0 else None

        return cls(
            model=model,
            data=data,
            left_id=left_id,
            right_id=right_id,
            option=option,
            scene=scene,
            context=context,
            camera=camera,
            spin_qposadr=spin_qposadr,
            calib_left_x=calib_left_x,
            calib_left_y=calib_left_y,
            calib_right_x=calib_right_x,
            calib_right_y=calib_right_y,
            clear_r=clear_rgb[0],
            clear_g=clear_rgb[1],
            clear_b=clear_rgb[2],
        )

    def step(self, t: float, animate: bool) -> None:
        if animate and self.spin_qposadr is not None:
            self.data.qpos[self.spin_qposadr] = 0.9 * math.sin(1.25 * t)
            mujoco.mj_forward(self.model, self.data)
        else:
            mujoco.mj_step(self.model, self.data)

    def _camera_for_eye(self, view_index: int, state: RuntimeComfortState) -> int:
        if state.mono_to_both_eyes:
            return self.left_id

        if view_index == 0:
            return self.right_id if state.swap_eyes else self.left_id

        return self.left_id if state.swap_eyes else self.right_id

    def _calib_for_eye(self, view_index: int) -> tuple[int, int]:
        if view_index == 0:
            return self.calib_left_x, self.calib_left_y

        return self.calib_right_x, self.calib_right_y

    def _comfort_shift_for_eye(self, view_index: int, state: RuntimeComfortState) -> float:
        # Positive scene_farther_px:
        #   left eye image shifts left,
        #   right eye image shifts right,
        #   usually perceived as farther / less crossed.
        sign = -1.0 if state.invert_scene_shift else 1.0
        half_farther = 0.5 * state.scene_farther_px * sign

        if view_index == 0:
            return -half_farther

        return half_farther

    def render_eye(self, view_index: int, state: RuntimeComfortState) -> None:
        from OpenGL import GL

        cam_id = self._camera_for_eye(view_index, state)

        self.camera.type = mujoco.mjtCamera.mjCAMERA_FIXED
        self.camera.fixedcamid = cam_id

        mujoco.mjv_updateScene(
            self.model,
            self.data,
            self.option,
            None,
            self.camera,
            mujoco.mjtCatBit.mjCAT_ALL,
            self.scene,
        )

        viewport = GL.glGetIntegerv(GL.GL_VIEWPORT)
        vp_x = int(viewport[0])
        vp_y = int(viewport[1])
        vp_w = int(viewport[2])
        vp_h = int(viewport[3])

        calib_x, calib_y = self._calib_for_eye(view_index)
        comfort_x = self._comfort_shift_for_eye(view_index, state)

        _drain_gl_errors(f"before render_eye {view_index}")

        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glClearColor(self.clear_r, self.clear_g, self.clear_b, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)

        rect = mujoco.MjrRect(
            int(round(vp_x + calib_x + comfort_x)),
            int(round(vp_y + calib_y)),
            vp_w,
            vp_h,
        )

        mujoco.mjr_render(rect, self.scene, self.context)

        _drain_gl_errors(f"after mujoco.mjr_render eye {view_index}", print_limit=2)

    def close(self) -> None:
        self.context.free()


def parse_args():
    from .config_util import load_calibration, load_comfort

    calib = load_calibration()
    comfort = load_comfort()

    parser = argparse.ArgumentParser(
        description="Render MuJoCo stereo cameras to OpenXR OpenGL swapchain via headless EGL, with comfort controls."
    )

    parser.add_argument(
        "--model",
        default=str(Path(__file__).resolve().parent.parent / "models" / "stereo_endoscope_test.xml"),
        help="Path to MuJoCo XML model.",
    )
    parser.add_argument("--left-camera", default="endo_left")
    parser.add_argument("--right-camera", default="endo_right")
    parser.add_argument("--max-geom", type=int, default=10000)

    parser.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="0 means run forever",
    )
    parser.add_argument(
        "--swap-eyes", action="store_true",
        default=comfort.swap_eyes if comfort else False,
    )
    parser.add_argument("--no-animate", action="store_true")
    parser.add_argument("--clear-only", action="store_true")
    parser.add_argument("--print-every", type=float, default=2.0)

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

    # Comfort / depth controls.
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

    return parser.parse_args()


def check_openxr() -> None:
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


def clear_eye(view_index: int) -> None:
    from OpenGL import GL

    if view_index == 0:
        GL.glClearColor(0.90, 0.10, 0.10, 1.0)
    else:
        GL.glClearColor(0.10, 0.20, 0.90, 1.0)

    GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)


def main() -> int:
    args = parse_args()

    print("[INFO] Starting MuJoCo -> pyopenxr OpenGL (headless EGL).")
    print("[INFO] HMD pose is ignored; MuJoCo fixed cameras define the views.")
    print("[INFO] Comfort features: mono-to-both-eyes + stereo scene farther/nearer shift.")

    from .config_util import _default_config_path

    cfg_path = _default_config_path()
    has_calib = any((args.calib_left_x, args.calib_left_y, args.calib_right_x, args.calib_right_y))
    has_comfort_cfg_src = cfg_path.exists()

    if has_calib or has_comfort_cfg_src:
        src = f"from {cfg_path}" if cfg_path.exists() else "from CLI"
        if has_calib:
            print(
                f"[INFO] Calibration ({src}): "
                f"L=({args.calib_left_x:+d}, {args.calib_left_y:+d})  "
                f"R=({args.calib_right_x:+d}, {args.calib_right_y:+d})"
            )

    print(HINT)

    comfort_state = RuntimeComfortState(
        swap_eyes=args.swap_eyes,
        mono_to_both_eyes=args.mono_to_both_eyes,
        scene_farther_px=args.scene_farther_px,
        scene_shift_step_px=args.scene_shift_step_px,
        max_abs_scene_shift_px=args.max_scene_shift_px,
        invert_scene_shift=args.invert_scene_shift,
    )
    comfort_state.clamp()

    _print_comfort_state("[COMFORT]", comfort_state)

    # Important: create EGL context before touching pyopenxr ContextObject.
    provider = _NvidiaEGLContextProvider()

    check_openxr()

    from xr.utils.gl import ContextObject

    renderer = None
    stdin_reader = _StdinReader()

    start = time.perf_counter()
    last = start
    frames = 0
    running = True

    try:
        with ContextObject(
            context_provider=provider,
            instance_create_info=xr.InstanceCreateInfo(
                enabled_extension_names=list(_REQUIRED_EXTENSIONS),
            ),
        ) as xr_context:

            if not args.clear_only:
                provider.make_current()
                _drain_gl_errors("before MujocoStereoRenderer.create")

                renderer = MujocoStereoRenderer.create(
                    args.model,
                    args.left_camera,
                    args.right_camera,
                    args.max_geom,
                    calib_left_x=args.calib_left_x,
                    calib_left_y=args.calib_left_y,
                    calib_right_x=args.calib_right_x,
                    calib_right_y=args.calib_right_y,
                    clear_rgb=tuple(args.clear_rgb),
                )

                _drain_gl_errors("after MujocoStereoRenderer.create")

            try:
                for frame_index, frame_state in enumerate(xr_context.frame_loop()):
                    if not running:
                        break

                    t = time.perf_counter() - start

                    if renderer is not None:
                        renderer.step(
                            t,
                            animate=not args.no_animate,
                        )

                    for view_index, _view in enumerate(xr_context.view_loop(frame_state)):
                        if renderer is None:
                            clear_eye(view_index)
                        else:
                            renderer.render_eye(view_index, comfort_state)

                    frames += 1
                    now = time.perf_counter()

                    if now - last >= args.print_every:
                        fps = frames / (now - last)
                        print(
                            f"[INFO] FPS: {fps:.1f} "
                            f"SCENE_FARTHER={comfort_state.scene_farther_px:+.1f}px "
                            f"MONO={'on' if comfort_state.mono_to_both_eyes else 'off'} "
                            f"SWAP={'on' if comfort_state.swap_eyes else 'off'}"
                        )
                        frames = 0
                        last = now

                    running = _handle_input(stdin_reader, comfort_state)

                    if args.max_frames > 0 and frame_index + 1 >= args.max_frames:
                        break

            finally:
                if renderer is not None:
                    renderer.close()

    finally:
        stdin_reader.restore()
        provider.destroy()

    print("\n[RESULT] Final comfort state:")
    _print_comfort_state(" ", comfort_state)

    if args.save:
        from .config_util import Calibration, ComfortConfig, save_full_config

        cal = Calibration(
            left_x=args.calib_left_x,
            left_y=args.calib_left_y,
            right_x=args.calib_right_x,
            right_y=args.calib_right_y,
        )
        comf = ComfortConfig(
            mono_to_both_eyes=comfort_state.mono_to_both_eyes,
            swap_eyes=comfort_state.swap_eyes,
            scene_farther_px=comfort_state.scene_farther_px,
            scene_shift_step_px=comfort_state.scene_shift_step_px,
            max_abs_scene_shift_px=comfort_state.max_abs_scene_shift_px,
            invert_scene_shift=comfort_state.invert_scene_shift,
            clear_r=renderer.clear_r if renderer else 0.02,
            clear_g=renderer.clear_g if renderer else 0.02,
            clear_b=renderer.clear_b if renderer else 0.02,
        )
        from .config_util import _default_config_path
        save_full_config(cal, comf)
        print(f"[INFO] Saved calibration + comfort to {_default_config_path()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
