from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
os.environ.setdefault("MUJOCO_GL", "egl")

import mujoco as mj
import numpy as np
import xr

ROBOT_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "surgical_continuum_robot"
sys.path.insert(0, str(ROBOT_PROJECT_ROOT / "remote_control_ex"))

from .xr_mujoco_opengl_comfort import (  # noqa: E402
    _NvidiaEGLContextProvider,
    _StdinReader,
    _REQUIRED_EXTENSIONS,
    _drain_gl_errors,
    _extension_name_to_str,
    _print_comfort_state,
    _handle_input,
    RuntimeComfortState,
    HINT,
    clear_eye,
    check_openxr,
)

from .config_util import (  # noqa: E402
    Calibration,
    ComfortConfig,
    load_calibration,
    load_comfort,
    save_full_config,
    _default_config_path,
)

from teleop_core.scene import MujocoScene  # noqa: E402
from teleop_core.config import (  # noqa: E402
    AppConfig,
    SimulationConfig,
    ControllerConfig,
    IKConfig,
    KinematicsConfig,
    ModelNames,
)
from teleop_core.controller import ContinuumController  # noqa: E402
from teleop_core.ik import DampedLeastSquaresIK  # noqa: E402
from teleop_core.kinematics import ContinuumKinematics  # noqa: E402
from teleop_core.runtime import MujocoStepper  # noqa: E402


DEFAULT_MODEL = "scene_single_arm.xml"
DEFAULT_LEFT_CAM = "up_cam1"
DEFAULT_RIGHT_CAM = "up_cam2"
MAX_GEOM = 50000


@dataclass
class SurgicalStereoRenderer:
    model: mj.MjModel
    data: mj.MjData
    left_id: int
    right_id: int
    scene: mj.MjvScene
    mjr_context: mj.MjrContext
    camera: mj.MjvCamera
    calib_left_x: int = 0
    calib_left_y: int = 0
    calib_right_x: int = 0
    calib_right_y: int = 0
    clear_r: float = 0.02
    clear_g: float = 0.02
    clear_b: float = 0.02

    @classmethod
    def create(
        cls,
        model: mj.MjModel,
        data: mj.MjData,
        left_camera: str,
        right_camera: str,
        calib_left_x: int = 0,
        calib_left_y: int = 0,
        calib_right_x: int = 0,
        calib_right_y: int = 0,
        clear_rgb: tuple[float, float, float] = (0.02, 0.02, 0.02),
    ):
        left_id = mj.mj_name2id(model, mj.mjtObj.mjOBJ_CAMERA, left_camera)
        right_id = mj.mj_name2id(model, mj.mjtObj.mjOBJ_CAMERA, right_camera)
        if left_id < 0:
            raise RuntimeError(f"Camera not found: {left_camera}")
        if right_id < 0:
            raise RuntimeError(f"Camera not found: {right_camera}")

        option = mj.MjvOption()

        _drain_gl_errors("before MjrContext (surgical)")
        mjr_context = mj.MjrContext(model, mj.mjtFontScale.mjFONTSCALE_150)
        _drain_gl_errors("after MjrContext (surgical)")

        scene = mj.MjvScene(model, maxgeom=MAX_GEOM)
        camera = mj.MjvCamera()

        return cls(
            model=model,
            data=data,
            left_id=left_id,
            right_id=right_id,
            scene=scene,
            mjr_context=mjr_context,
            camera=camera,
            calib_left_x=calib_left_x,
            calib_left_y=calib_left_y,
            calib_right_x=calib_right_x,
            calib_right_y=calib_right_y,
            clear_r=clear_rgb[0],
            clear_g=clear_rgb[1],
            clear_b=clear_rgb[2],
        )

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
        sign = -1.0 if state.invert_scene_shift else 1.0
        half = 0.5 * state.scene_farther_px * sign
        if view_index == 0:
            return -half
        return half

    def render_eye(self, view_index: int, state: RuntimeComfortState) -> None:
        from OpenGL import GL

        cam_id = self._camera_for_eye(view_index, state)

        self.camera.type = mj.mjtCamera.mjCAMERA_FIXED
        self.camera.fixedcamid = cam_id

        original_fovy = float(self.model.cam_fovy[cam_id])
        self.model.cam_fovy[cam_id] = original_fovy / max(state.zoom, 0.1)

        mj.mjv_updateScene(
            self.model,
            self.data,
            mj.MjvOption(),
            None,
            self.camera,
            mj.mjtCatBit.mjCAT_ALL,
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

        rect = mj.MjrRect(
            int(round(vp_x + calib_x + comfort_x)),
            int(round(vp_y + calib_y)),
            vp_w,
            vp_h,
        )

        mj.mjr_render(rect, self.scene, self.mjr_context)

        _drain_gl_errors(f"after mjr_render eye {view_index}", print_limit=2)

    def close(self) -> None:
        self.mjr_context.free()


class SurgicalSimLoop:
    def __init__(
        self,
        scene: MujocoScene,
        controller: ContinuumController,
        config: SimulationConfig,
        scene_lock: threading.Lock,
    ):
        self.scene = scene
        self.controller = controller
        self.config = config
        self.scene_lock = scene_lock
        self.stepper = MujocoStepper()

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            with self.scene_lock:
                position_goal, _, rotation_goal = self.scene.target_pose_in_base()
                self.controller.step(position_goal, rotation_goal, 0.0)
                self.stepper.step(
                    self.scene.model,
                    self.scene.data,
                    self.config.decimation,
                )
            stop_event.wait(self.config.control_period)


def parse_args():
    calib = load_calibration()
    comfort = load_comfort()

    parser = argparse.ArgumentParser(
        description="Surgical continuum robot stereo rendering to OpenXR (VIVE)."
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="MuJoCo model XML filename (in robot project mjcf dir).",
    )
    parser.add_argument("--left-camera", default=DEFAULT_LEFT_CAM)
    parser.add_argument("--right-camera", default=DEFAULT_RIGHT_CAM)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--print-every", type=float, default=2.0)

    parser.add_argument(
        "--swap-eyes", action="store_true",
        default=comfort.swap_eyes if comfort else False,
    )
    parser.add_argument(
        "--mono-to-both-eyes", action="store_true",
        default=comfort.mono_to_both_eyes if comfort else False,
    )
    parser.add_argument(
        "--scene-farther-px", type=float,
        default=comfort.scene_farther_px if comfort else 0.0,
    )
    parser.add_argument(
        "--scene-shift-step-px", type=float,
        default=comfort.scene_shift_step_px if comfort else 2.0,
    )
    parser.add_argument(
        "--max-scene-shift-px", type=float,
        default=comfort.max_abs_scene_shift_px if comfort else 80.0,
    )
    parser.add_argument(
        "--invert-scene-shift", action="store_true",
        default=comfort.invert_scene_shift if comfort else False,
    )
    parser.add_argument(
        "--zoom", type=float,
        default=comfort.zoom if comfort else 1.0,
        help="FOV zoom factor. >1 = wider FOV (see more).",
    )
    parser.add_argument(
        "--zoom-step", type=float,
        default=comfort.zoom_step if comfort else 0.1,
        help="Keyboard zoom adjustment step.",
    )
    parser.add_argument("--clear-rgb", type=float, nargs=3, metavar=("R", "G", "B"),
                        default=comfort.to_rgb_tuple() if comfort else (0.02, 0.02, 0.02))
    parser.add_argument("--calib-left-x", type=int,
                        default=calib.left_x if calib else 0)
    parser.add_argument("--calib-left-y", type=int,
                        default=calib.left_y if calib else 0)
    parser.add_argument("--calib-right-x", type=int,
                        default=calib.right_x if calib else 0)
    parser.add_argument("--calib-right-y", type=int,
                        default=calib.right_y if calib else 0)
    parser.add_argument("--save", action="store_true",
                        help="Save calibration and comfort to config on exit.")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("[INFO] Surgical Robot — OpenXR Stereo (VIVE)")
    print("[INFO] Model:", args.model)
    print("[INFO] Cameras:", args.left_camera, "/", args.right_camera)
    print(HINT)

    cfg_path = _default_config_path()
    src = f"from {cfg_path}" if cfg_path.exists() else "defaults"
    has_calib = any((args.calib_left_x, args.calib_left_y, args.calib_right_x, args.calib_right_y))
    if has_calib:
        print(f"[INFO] Calibration ({src}): "
              f"L=({args.calib_left_x:+d}, {args.calib_left_y:+d})  "
              f"R=({args.calib_right_x:+d}, {args.calib_right_y:+d})")

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

    mjcf_dir = ROBOT_PROJECT_ROOT / "model" / "continuum_robot" / "mjcf"
    xml_path = mjcf_dir / args.model
    if not xml_path.exists():
        raise FileNotFoundError(f"Model not found: {xml_path}")

    sim_config = SimulationConfig()
    names = ModelNames()
    scene = MujocoScene.load(xml_path, sim_config, names)

    kinematics = ContinuumKinematics(KinematicsConfig())
    ik = DampedLeastSquaresIK(kinematics, IKConfig(), task_mode="pos_z")
    controller = ContinuumController(scene, ik, ControllerConfig())

    scene.initialize_mocap_to_tip()
    print("[INFO] Robot controller initialized. Target stays at initial tip pose.")
    print("[INFO] Drag target_body in mujoco viewer for interaction, or connect Omega.7.")

    provider = _NvidiaEGLContextProvider()
    check_openxr()

    from xr.utils.gl import ContextObject

    renderer = None
    stdin_reader = _StdinReader()
    scene_lock = threading.Lock()
    stop_event = threading.Event()

    sim_loop = SurgicalSimLoop(scene, controller, sim_config, scene_lock)
    sim_thread = threading.Thread(
        target=sim_loop.run,
        args=(stop_event,),
        daemon=True,
        name="surgical-sim",
    )

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
            provider.make_current()
            _drain_gl_errors("before SurgicalStereoRenderer.create")

            renderer = SurgicalStereoRenderer.create(
                scene.model,
                scene.data,
                args.left_camera,
                args.right_camera,
                calib_left_x=args.calib_left_x,
                calib_left_y=args.calib_left_y,
                calib_right_x=args.calib_right_x,
                calib_right_y=args.calib_right_y,
                clear_rgb=tuple(args.clear_rgb),
            )
            _drain_gl_errors("after SurgicalStereoRenderer.create")

            sim_thread.start()
            print("[INFO] Simulation thread started.")

            # Print camera diagnostic
            mj.mj_forward(scene.model, scene.data)
            for cam_name in (args.left_camera, args.right_camera):
                cam_id = mj.mj_name2id(scene.model, mj.mjtObj.mjOBJ_CAMERA, cam_name)
                cam_pos = scene.data.cam_xpos[cam_id]
                cam_mat = scene.data.cam_xmat[cam_id].reshape(3, 3)
                cam_z = cam_mat[:, 2]
                tip_pos = scene.data.site_xpos[scene.ids.tip_site]
                print(f"[INFO] {cam_name}: pos={cam_pos} zaxis={cam_z} tip_dist={np.linalg.norm(tip_pos - cam_pos):.3f}m")
            print(f"[INFO] Tip site: {tip_pos}")
            print(f"[INFO] Scene geoms rendered: {renderer.scene.ngeom}")
            print("[INFO] If screen is black, try: --clear-rgb 0.3 0.3 0.4 to brighten background")

            for frame_index, frame_state in enumerate(xr_context.frame_loop()):
                if not running:
                    break

                for view_index, _view in enumerate(xr_context.view_loop(frame_state)):
                    with scene_lock:
                        renderer.render_eye(view_index, comfort_state)

                frames += 1
                now = time.perf_counter()
                if now - last >= args.print_every:
                    dt = now - last
                    print(
                        f"[INFO] FPS: {frames / dt:.1f}  "
                        f"ZOOM={comfort_state.zoom:.2f}  "
                        f"SCENE_FARTHER={comfort_state.scene_farther_px:+.1f}px  "
                        f"MONO={'on' if comfort_state.mono_to_both_eyes else 'off'}  "
                        f"SWAP={'on' if comfort_state.swap_eyes else 'off'}"
                    )
                    frames = 0
                    last = now

                running = _handle_input(stdin_reader, comfort_state)

                if args.max_frames > 0 and frame_index + 1 >= args.max_frames:
                    break

    finally:
        stop_event.set()
        stdin_reader.restore()
        if renderer is not None:
            renderer.close()
        provider.destroy()
        print("\n[RESULT] Final state:")
        _print_comfort_state(" ", comfort_state)

        if args.save:
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
            print(f"[INFO] Saved to {_default_config_path()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
