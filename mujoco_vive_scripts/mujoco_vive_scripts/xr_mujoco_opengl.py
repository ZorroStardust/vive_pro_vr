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
from dataclasses import dataclass, field
from pathlib import Path

# Env vars deferred: EGL is set inside OpenXRSink; GLFW uses platform default.
# Do not import OpenGL.GL at module top.

import mujoco

from .xr_common import (
    HINT,
    RuntimeComfortState,
    _StdinReader,
    _drain_gl_errors,
    _get_gl,
    _handle_input,
    _print_comfort_state,
    add_calibration_args,
    add_comfort_args,
    add_screen_args,
    make_sink,
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
class RenderTimings:
    """Per-eye timing breakdown aggregated by ``MujocoStereoRenderer``.

    Always populated; ``reset()`` is called by the main loop at each
    ``print_every`` boundary so averages stay windowed.
    """

    eye_count: int = 0
    update_scene_total: float = 0.0
    mjr_render_total: float = 0.0
    other_total: float = 0.0

    def add(self, *, update_scene: float, mjr_render: float, other: float) -> None:
        self.eye_count += 1
        self.update_scene_total += update_scene
        self.mjr_render_total += mjr_render
        self.other_total += other

    def avg_ms(self) -> dict[str, float]:
        if self.eye_count == 0:
            return {"update_scene": 0.0, "mjr_render": 0.0, "other": 0.0}
        n = self.eye_count
        return {
            "update_scene": self.update_scene_total / n * 1000.0,
            "mjr_render": self.mjr_render_total / n * 1000.0,
            "other": self.other_total / n * 1000.0,
        }

    def reset(self) -> None:
        self.eye_count = 0
        self.update_scene_total = 0.0
        self.mjr_render_total = 0.0
        self.other_total = 0.0


class _MonoFBOBlitter:
    """Lazy offscreen FBO used by the mono-to-both-eyes fast path.

    The first call to :py:meth:`ensure` allocates the FBO at the requested
    size; subsequent calls with the same size are a no-op.  A different size
    triggers a delete + recreate.
    """

    def __init__(self) -> None:
        self._fbo = 0
        self._color = 0
        self._depth = 0
        self._w = 0
        self._h = 0

    def ensure(self, w: int, h: int, GL) -> None:
        if self._fbo and self._w == w and self._h == h:
            return

        self.delete(GL)

        self._fbo = int(GL.glGenFramebuffers(1))
        self._color = int(GL.glGenTextures(1))
        self._depth = int(GL.glGenRenderbuffers(1))

        GL.glBindTexture(GL.GL_TEXTURE_2D, self._color)
        GL.glTexImage2D(
            GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0,
            GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None,
        )
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)

        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, self._depth)
        GL.glRenderbufferStorage(GL.GL_RENDERBUFFER, GL.GL_DEPTH_COMPONENT24, w, h)

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._fbo)
        GL.glFramebufferTexture2D(
            GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
            GL.GL_TEXTURE_2D, self._color, 0,
        )
        GL.glFramebufferRenderbuffer(
            GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT,
            GL.GL_RENDERBUFFER, self._depth,
        )

        status = int(GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER))
        if status != int(GL.GL_FRAMEBUFFER_COMPLETE):
            self.delete(GL)
            raise RuntimeError(f"Mono FBO incomplete: 0x{status:x}")

        self._w = w
        self._h = h
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)

    def bind_for_render(self, GL) -> None:
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._fbo)
        GL.glViewport(0, 0, self._w, self._h)

    def blit_to(self, GL, dst_x: int, dst_y: int, dst_w: int, dst_h: int) -> None:
        """Blit the full FBO colour buffer to ``(dst_x..dst_x+dst_w, dst_y..dst_y+dst_h)``.

        Caller is responsible for binding the destination framebuffer (the
        swapchain FBO / default framebuffer) beforehand.
        """
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self._fbo)
        GL.glBlitFramebuffer(
            0, 0, self._w, self._h,
            int(dst_x), int(dst_y),
            int(dst_x + dst_w), int(dst_y + dst_h),
            GL.GL_COLOR_BUFFER_BIT,
            GL.GL_LINEAR,
        )

    def delete(self, GL) -> None:
        if self._fbo:
            try:
                GL.glDeleteFramebuffers(1, [self._fbo])
            except Exception:
                pass
            self._fbo = 0
        if self._color:
            try:
                GL.glDeleteTextures(1, [self._color])
            except Exception:
                pass
            self._color = 0
        if self._depth:
            try:
                GL.glDeleteRenderbuffers(1, [self._depth])
            except Exception:
                pass
            self._depth = 0
        self._w = 0
        self._h = 0


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

    # Mutable state.  These are dataclass fields (not class attrs) so the
    # ``dataclass`` decorator's default_factory applies.  ``timings`` is
    # always populated by ``render_eye``; ``mono_fbo`` is lazily created on
    # the first mono-fast frame.
    timings: RenderTimings = field(default_factory=RenderTimings)
    mono_fbo: _MonoFBOBlitter | None = None

    _pre_cleared: bool = False

    def begin_frame(self) -> None:
        """Call once per frame before the eye loop starts."""
        pass

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

    def _viewport_for(self, view_index: int, GL) -> tuple[int, int, int, int]:
        """Return ``(vp_x, vp_y, vp_w, vp_h)`` for the current viewport."""
        vp = GL.glGetIntegerv(GL.GL_VIEWPORT)
        return (int(vp[0]), int(vp[1]), int(vp[2]), int(vp[3]))

    def render_eye(self, view_index: int, state: RuntimeComfortState) -> None:
        """Render one eye's view to the currently-bound swapchain FBO.

        When ``state.mono_fast and state.mono_to_both_eyes`` are both True,
        the first eye renders the MuJoCo scene to an offscreen FBO and blits
        it to the eye's viewport; the second eye skips the scene rebuild and
        only blits the cached FBO.  This halves ``mjv_updateScene`` and
        ``mjr_render`` work in the common mono diagnostic mode.
        """
        GL = _get_gl()
        timing_t0 = time.perf_counter()

        use_mono_fast = bool(state.mono_fast and state.mono_to_both_eyes)

        cam_id = self._camera_for_eye(view_index, state)

        self.camera.type = mujoco.mjtCamera.mjCAMERA_FIXED
        self.camera.fixedcamid = cam_id

        original_fovy = float(self.model.cam_fovy[cam_id])
        self.model.cam_fovy[cam_id] = original_fovy / max(state.zoom, 0.1)

        if use_mono_fast and view_index == 1:
            # Skip scene rebuild: scene contents are identical for both eyes.
            update_scene_dt = 0.0
            mjr_render_dt = 0.0

            self.model.cam_fovy[cam_id] = original_fovy

            vp_x, vp_y, vp_w, vp_h = self._viewport_for(view_index, GL)
            calib_x, calib_y = self._calib_for_eye(view_index)
            comfort_x = self._comfort_shift_for_eye(view_index, state)

            assert self.mono_fbo is not None
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
            GL.glViewport(vp_x, vp_y, vp_w, vp_h)
            self.mono_fbo.blit_to(
                GL,
                int(round(vp_x + calib_x + comfort_x)),
                int(round(vp_y + calib_y)),
                vp_w,
                vp_h,
            )
            t_end = time.perf_counter()
        else:
            mujoco.mjv_updateScene(
                self.model,
                self.data,
                self.option,
                None,
                self.camera,
                mujoco.mjtCatBit.mjCAT_ALL,
                self.scene,
            )
            t_post_us = time.perf_counter()

            self.model.cam_fovy[cam_id] = original_fovy

            vp_x, vp_y, vp_w, vp_h = self._viewport_for(view_index, GL)
            calib_x, calib_y = self._calib_for_eye(view_index)
            comfort_x = self._comfort_shift_for_eye(view_index, state)

            if use_mono_fast:
                assert view_index == 0
                if self.mono_fbo is None:
                    self.mono_fbo = _MonoFBOBlitter()
                self.mono_fbo.ensure(vp_w, vp_h, GL)
                self.mono_fbo.bind_for_render(GL)

            GL.glEnable(GL.GL_DEPTH_TEST)
            if not self._pre_cleared:
                GL.glClearColor(self.clear_r, self.clear_g, self.clear_b, 1.0)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)

            if use_mono_fast:
                rect = mujoco.MjrRect(0, 0, vp_w, vp_h)
            else:
                rect = mujoco.MjrRect(
                    int(round(vp_x + calib_x + comfort_x)),
                    int(round(vp_y + calib_y)),
                    vp_w,
                    vp_h,
                )

            t_pre_r = time.perf_counter()
            mujoco.mjr_render(rect, self.scene, self.context)
            t_post_r = time.perf_counter()

            _drain_gl_errors(f"after mujoco.mjr_render eye {view_index}", print_limit=2)

            if use_mono_fast:
                # Switch back to default framebuffer (the swapchain image) and
                # blit the cached scene texture to the eye viewport with offset.
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
                GL.glViewport(vp_x, vp_y, vp_w, vp_h)
                assert self.mono_fbo is not None
                self.mono_fbo.blit_to(
                    GL,
                    int(round(vp_x + calib_x + comfort_x)),
                    int(round(vp_y + calib_y)),
                    vp_w,
                    vp_h,
                )

            t_end = time.perf_counter()
            update_scene_dt = t_post_us - timing_t0
            mjr_render_dt = t_post_r - t_pre_r

        other_dt = t_end - timing_t0 - update_scene_dt - mjr_render_dt
        if other_dt < 0:
            other_dt = 0.0

        self.timings.add(
            update_scene=update_scene_dt,
            mjr_render=mjr_render_dt,
            other=other_dt,
        )

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
    parser.add_argument(
        "--mono-fast",
        action="store_true",
        help=(
            "Optimisation: when --mono-to-both-eyes is active, render the "
            "MuJoCo scene once into an offscreen FBO and blit to both eye "
            "viewports. Halves mjv_updateScene / mjr_render cost at the "
            "price of one GL FBO + two blits per frame."
        ),
    )

    add_calibration_args(parser, calib)
    add_comfort_args(parser, comfort)
    add_screen_args(parser)

    return parser.parse_args()


def clear_eye(view_index: int) -> None:
    GL = _get_gl()

    if view_index == 0:
        GL.glClearColor(0.90, 0.10, 0.10, 1.0)
    else:
        GL.glClearColor(0.10, 0.20, 0.90, 1.0)

    GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)


def _print_banner(args: argparse.Namespace, cfg_path: Path) -> None:
    mode = "SCREEN (GLFW window)" if args.screen else "OpenXR (headless EGL)"
    print(f"[INFO] Starting MuJoCo stereo — {mode}.")
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
        mono_fast=args.mono_fast,
        scene_farther_px=args.scene_farther_px,
        scene_shift_step_px=args.scene_shift_step_px,
        max_abs_scene_shift_px=args.max_scene_shift_px,
        invert_scene_shift=args.invert_scene_shift,
        zoom=args.zoom,
        zoom_step=args.zoom_step,
    )
    comfort_state.clamp()

    _print_comfort_state("[COMFORT]", comfort_state)

    sink = make_sink(args)

    renderer = None
    stdin_reader = _StdinReader()

    start = time.perf_counter()
    last = start
    frames = 0
    frame_index = 0
    running = True

    try:
        with sink as s:

            if not args.clear_only:
                s.make_current()
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

            if args.screen and renderer is not None:
                renderer._pre_cleared = True

            try:
                for frame_state in s.frame_loop():
                    if not running:
                        break

                    t_frame_start = time.perf_counter()
                    t = t_frame_start - start

                    if renderer is not None:
                        renderer.begin_frame()
                        t_step_start = time.perf_counter()
                        renderer.step(
                            t,
                            animate=not args.no_animate,
                        )
                        t_step_end = time.perf_counter()

                    for view_index, _view in enumerate(s.view_loop(frame_state)):
                        if renderer is None:
                            clear_eye(view_index)
                        else:
                            renderer.render_eye(view_index, comfort_state)

                    frames += 1
                    frame_index += 1
                    now = time.perf_counter()

                    if now - last >= args.print_every:
                        fps = frames / (now - last)
                        frame_total_ms = (now - t_frame_start) * 1000.0
                        step_ms = (t_step_end - t_step_start) * 1000.0 if renderer is not None else 0.0
                        avg = renderer.timings.avg_ms() if renderer is not None else {"update_scene": 0.0, "mjr_render": 0.0, "other": 0.0}
                        # Per-eye averages × 2 eyes + step = approximate render budget.
                        render_eye_total_ms = 2.0 * (avg['update_scene'] + avg['mjr_render'] + avg['other'])
                        outside_ms = max(0.0, frame_total_ms - step_ms - render_eye_total_ms)
                        mono_tag = "MFAST" if comfort_state.mono_fast else "    "
                        print(
                            f"[INFO] FPS: {fps:5.1f}  "
                            f"FRM={frame_total_ms:5.2f}ms  "
                            f"STEP={step_ms:4.2f}  "
                            f"UPD={avg['update_scene']*2:4.2f}  "
                            f"REN={avg['mjr_render']*2:4.2f}  "
                            f"OTH={avg['other']*2:4.2f}  "
                            f"OUT={outside_ms:4.2f}  "
                            f"ZOOM={comfort_state.zoom:.2f}  "
                            f"SF={comfort_state.scene_farther_px:+.1f}px  "
                            f"{mono_tag} "
                            f"M={'on' if comfort_state.mono_to_both_eyes else 'off '}  "
                            f"S={'on' if comfort_state.swap_eyes else 'off '}"
                        )
                        if renderer is not None:
                            renderer.timings.reset()
                        frames = 0
                        last = now

                    running = _handle_input(stdin_reader, comfort_state)

                    if args.max_frames > 0 and frame_index >= args.max_frames:
                        break

            finally:
                if renderer is not None:
                    renderer.close()

    finally:
        stdin_reader.restore()

    print("\n[RESULT] Final comfort state:")
    _print_comfort_state(" ", comfort_state)

    _save_final_state(args, comfort_state, renderer)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
