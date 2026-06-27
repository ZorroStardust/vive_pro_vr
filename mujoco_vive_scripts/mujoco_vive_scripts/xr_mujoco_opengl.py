"""Render MuJoCo stereo cameras to OpenXR OpenGL swapchain via headless EGL.

This is the core entry point of the package: it owns the ``MujocoStereoRenderer``
class, the headless EGL context creation, the comfort control loop and the
shared main render loop.  Standalone scripts (``xr_surgical_robot``) reuse
``MujocoStereoRenderer`` via inheritance; other tools (``xr_crosshair_*``,
``xr_pink_world_check``) reuse the helpers from ``xr_common``.

HMD pose is ignored: MuJoCo fixed cameras define the views.  All comfort
parameters are persisted via ``config_util`` and the ``calibration.toml`` file.
"""

from __future__ import annotations

import argparse
import math
import os
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

from .xr_common import (
    HINT,
    RuntimeComfortState,
    _NvidiaEGLContextProvider,
    _REQUIRED_EXTENSIONS,
    _StdinReader,
    _drain_gl_errors,
    _get_gl,
    _handle_input,
    _print_comfort_state,
    add_calibration_args,
    add_comfort_args,
    check_openxr,
)
from .config_util import (
    Calibration,
    ComfortConfig,
    _default_config_path,
    load_calibration,
    load_comfort,
    save_full_config,
)


@dataclass
class MujocoStereoRenderer:
    """OpenXR stereo renderer driven by a fixed pair of MuJoCo cameras.

    For external simulations where the physics step is owned by another thread,
    use a subclass (``SurgicalStereoRenderer``) that takes an existing
    ``model`` / ``data`` pair and never calls :py:meth:`step`.
    """

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

        left_id, right_id = cls._resolve_cameras(model, left_camera, right_camera)

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

    @staticmethod
    def _resolve_cameras(model, left_camera: str, right_camera: str) -> tuple[int, int]:
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
        return left_id, right_id

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
        GL = _get_gl()

        cam_id = self._camera_for_eye(view_index, state)

        self.camera.type = mujoco.mjtCamera.mjCAMERA_FIXED
        self.camera.fixedcamid = cam_id

        original_fovy = float(self.model.cam_fovy[cam_id])
        self.model.cam_fovy[cam_id] = original_fovy / max(state.zoom, 0.1)

        mujoco.mjv_updateScene(
            self.model,
            self.data,
            self.option,
            None,
            self.camera,
            mujoco.mjtCatBit.mjCAT_ALL,
            self.scene,
        )

        self.model.cam_fovy[cam_id] = original_fovy

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


# ---- CLI -----------------------------------------------------------------

def parse_args() -> argparse.Namespace:
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
    parser.add_argument("--no-animate", action="store_true")
    parser.add_argument("--clear-only", action="store_true")
    parser.add_argument("--print-every", type=float, default=2.0)

    add_calibration_args(parser, calib)
    add_comfort_args(parser, comfort)

    return parser.parse_args()


def clear_eye(view_index: int) -> None:
    GL = _get_gl()

    if view_index == 0:
        GL.glClearColor(0.90, 0.10, 0.10, 1.0)
    else:
        GL.glClearColor(0.10, 0.20, 0.90, 1.0)

    GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)


def _print_banner(args: argparse.Namespace, cfg_path: Path) -> None:
    print("[INFO] Starting MuJoCo -> pyopenxr OpenGL (headless EGL).")
    print("[INFO] HMD pose is ignored; MuJoCo fixed cameras define the views.")
    print("[INFO] Comfort features: mono-to-both-eyes + stereo scene farther/nearer shift.")

    has_calib = any(
        (args.calib_left_x, args.calib_left_y, args.calib_right_x, args.calib_right_y)
    )

    if has_calib:
        src = f"from {cfg_path}" if cfg_path.exists() else "from CLI"
        print(
            f"[INFO] Calibration ({src}): "
            f"L=({args.calib_left_x:+d}, {args.calib_left_y:+d})  "
            f"R=({args.calib_right_x:+d}, {args.calib_right_y:+d})"
        )

    print(HINT)


def _save_final_state(
    args: argparse.Namespace,
    comfort_state: RuntimeComfortState,
    renderer: MujocoStereoRenderer | None,
) -> None:
    if not args.save:
        return

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
        zoom=comfort_state.zoom,
        zoom_step=comfort_state.zoom_step,
        clear_r=renderer.clear_r if renderer else 0.02,
        clear_g=renderer.clear_g if renderer else 0.02,
        clear_b=renderer.clear_b if renderer else 0.02,
    )
    save_full_config(cal, comf)
    print(f"[INFO] Saved calibration + comfort to {_default_config_path()}")


def main() -> int:
    args = parse_args()

    cfg_path = _default_config_path()
    _print_banner(args, cfg_path)

    comfort_state = RuntimeComfortState(
        swap_eyes=args.swap_eyes,
        mono_to_both_eyes=args.mono_to_both_eyes,
        scene_farther_px=args.scene_farther_px,
        scene_shift_step_px=args.scene_shift_step_px,
        max_abs_scene_shift_px=args.max_scene_shift_px,
        invert_scene_shift=args.invert_scene_shift,
        zoom=args.zoom,
        zoom_step=args.zoom_step,
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
                            f"ZOOM={comfort_state.zoom:.2f} "
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

    _save_final_state(args, comfort_state, renderer)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
