from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
os.environ.setdefault("MUJOCO_GL", "egl")

import mujoco as mj
import xr

from .xr_common import (
    HINT,
    RuntimeComfortState,
    _NvidiaEGLContextProvider,
    _REQUIRED_EXTENSIONS,
    _StdinReader,
    _drain_gl_errors,
    _handle_input,
    _print_comfort_state,
    add_calibration_args,
    add_comfort_args,
    check_openxr,
)
from .xr_mujoco_opengl import MujocoStereoRenderer
from .config_util import (
    Calibration,
    ComfortConfig,
    _default_config_path,
    load_calibration,
    load_comfort,
    save_full_config,
)

ROBOT_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "surgical_continuum_robot"
sys.path.insert(0, str(ROBOT_PROJECT_ROOT / "remote_control_ex"))

from teleop_core.scene import MujocoScene  # noqa: E402
from teleop_core.config import (  # noqa: E402
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


class SurgicalStereoRenderer(MujocoStereoRenderer):
    """Render-only subclass of :class:`MujocoStereoRenderer` for shared model/data.

    The base class's :py:meth:`step` is intentionally not called: physics is
    driven by an external simulation thread that the caller owns.
    """

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
        left_id, right_id = cls._resolve_cameras(model, left_camera, right_camera)

        option = mj.MjvOption()

        _drain_gl_errors("before MjrContext (surgical)")
        context = mj.MjrContext(model, mj.mjtFontScale.mjFONTSCALE_150)
        _drain_gl_errors("after MjrContext (surgical)")

        scene = mj.MjvScene(model, maxgeom=MAX_GEOM)
        camera = mj.MjvCamera()

        return cls(
            model=model,
            data=data,
            left_id=left_id,
            right_id=right_id,
            option=option,
            scene=scene,
            context=context,
            camera=camera,
            spin_qposadr=None,
            calib_left_x=calib_left_x,
            calib_left_y=calib_left_y,
            calib_right_x=calib_right_x,
            calib_right_y=calib_right_y,
            clear_r=clear_rgb[0],
            clear_g=clear_rgb[1],
            clear_b=clear_rgb[2],
        )


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


def render_loop(
    model: mj.MjModel,
    data: mj.MjData,
    left_camera: str = DEFAULT_LEFT_CAM,
    right_camera: str = DEFAULT_RIGHT_CAM,
    comfort_state: RuntimeComfortState | None = None,
    stop_event: threading.Event | None = None,
    scene_lock: threading.Lock | None = None,
    calibr_left_x: int = 0,
    calibr_left_y: int = 0,
    calibr_right_x: int = 0,
    calibr_right_y: int = 0,
    clear_rgb: tuple[float, float, float] = (0.02, 0.02, 0.02),
    print_every: float = 2.0,
    print_diagnostics: bool = True,
    clutch_callback=None,
) -> None:
    """Run OpenXR stereo rendering loop on a shared MuJoCo model+data.

    This function blocks until the user quits (q/Esc) or *stop_event* is set.
    It only reads *model* / *data* and never writes to them, so an external
    simulation thread can drive physics concurrently through *scene_lock*.

    Parameters
    ----------
    model, data:
        The MuJoCo model and data owned by the caller.  These are read by
        ``mjv_updateScene`` / ``mjr_render`` but never mutated.
    left_camera, right_camera:
        Names of the fixed cameras to use for the left / right eye.
    comfort_state:
        Existing ``RuntimeComfortState`` instance; one is created from the
        config file if ``None``.
    stop_event:
        When set the render loop will exit cleanly at the next frame boundary.
    scene_lock:
        Shared lock used by the caller's simulation thread.  The render loop
        acquires it briefly during every ``render_eye`` call.
    calibr_*:
        Per-eye calibration offsets in pixels (overrides config file).
    clear_rgb:
        Background clear colour.
    print_every:
        Seconds between FPS / state diagnostic prints.
    print_diagnostics:
        Print camera diagnostic info at startup.
    """
    if comfort_state is None:
        comfort = load_comfort() or ComfortConfig()
        comfort_state = RuntimeComfortState(
            mono_to_both_eyes=comfort.mono_to_both_eyes,
            swap_eyes=comfort.swap_eyes,
            scene_farther_px=comfort.scene_farther_px,
            scene_shift_step_px=comfort.scene_shift_step_px,
            max_abs_scene_shift_px=comfort.max_abs_scene_shift_px,
            invert_scene_shift=comfort.invert_scene_shift,
            zoom=comfort.zoom,
            zoom_step=comfort.zoom_step,
        )
        comfort_state.clamp()

    if stop_event is None:
        stop_event = threading.Event()

    if scene_lock is None:
        scene_lock = threading.Lock()

    print("[INFO] VR render loop starting.")
    print(HINT)
    _print_comfort_state("[COMFORT]", comfort_state)

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
            provider.make_current()
            _drain_gl_errors("before SurgicalStereoRenderer.create")

            renderer = SurgicalStereoRenderer.create(
                model,
                data,
                left_camera,
                right_camera,
                calib_left_x=calibr_left_x,
                calib_left_y=calibr_left_y,
                calib_right_x=calibr_right_x,
                calib_right_y=calibr_right_y,
                clear_rgb=clear_rgb,
            )
            _drain_gl_errors("after SurgicalStereoRenderer.create")

            if print_diagnostics and scene_lock is not None:
                mj.mj_forward(model, data)
                for cam_name in (left_camera, right_camera):
                    cam_id = mj.mj_name2id(model, mj.mjtObj.mjOBJ_CAMERA, cam_name)
                    cam_pos = data.cam_xpos[cam_id]
                    cam_mat = data.cam_xmat[cam_id].reshape(3, 3)
                    cam_z = cam_mat[:, 2]
                    print(f"[INFO] {cam_name}: pos={cam_pos} zaxis={cam_z}")
                print(f"[INFO] Scene geoms: {renderer.scene.ngeom}")

            for _frame_index, frame_state in enumerate(xr_context.frame_loop()):
                if not running or stop_event.is_set():
                    break

                for view_index, _view in enumerate(xr_context.view_loop(frame_state)):
                    with scene_lock:
                        renderer.render_eye(view_index, comfort_state)

                frames += 1
                now = time.perf_counter()
                if now - last >= print_every:
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

                def _unhandled_cb(ch: str) -> bool:
                    if ch == " " and clutch_callback is not None:
                        clutch_callback()
                        return True
                    return True

                running = _handle_input(stdin_reader, comfort_state, _unhandled_cb)

    finally:
        stop_event.set()
        stdin_reader.restore()
        if renderer is not None:
            renderer.close()
        provider.destroy()
        print("\n[RESULT] Final state:")
        _print_comfort_state(" ", comfort_state)


def parse_args() -> argparse.Namespace:
    calib = load_calibration()
    comfort = load_comfort()

    parser = argparse.ArgumentParser(
        description="Surgical continuum robot stereo preview (standalone, with built-in sim)."
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

    add_calibration_args(parser, calib)
    add_comfort_args(parser, comfort)

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("[INFO] Surgical Robot — OpenXR Stereo preview (standalone)")
    print("[INFO] Model:", args.model)
    print("[INFO] Cameras:", args.left_camera, "/", args.right_camera)

    cfg_path = _default_config_path()
    has_calib = any(
        (args.calib_left_x, args.calib_left_y, args.calib_right_x, args.calib_right_y)
    )
    if has_calib:
        src = f"from {cfg_path}" if cfg_path.exists() else "CLI"
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

    if not ROBOT_PROJECT_ROOT.exists():
        raise FileNotFoundError(
            f"surgical_continuum_robot project not found at {ROBOT_PROJECT_ROOT}. "
            "This script requires the sibling teleop_core / remote_control_ex "
            "directories; either check out the robot project next to "
            "vive_pro_vr or use xr_mujoco_opengl.py for a self-contained demo."
        )

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
    print("[INFO] Standalone controller initialized (target at initial tip pose).")

    scene_lock = threading.Lock()
    stop_event = threading.Event()

    sim_loop = SurgicalSimLoop(scene, controller, sim_config, scene_lock)
    sim_thread = threading.Thread(
        target=sim_loop.run,
        args=(stop_event,),
        daemon=True,
        name="surgical-sim",
    )
    sim_thread.start()
    print("[INFO] Built-in simulation thread started.")

    render_loop(
        model=scene.model,
        data=scene.data,
        left_camera=args.left_camera,
        right_camera=args.right_camera,
        comfort_state=comfort_state,
        stop_event=stop_event,
        scene_lock=scene_lock,
        calibr_left_x=args.calib_left_x,
        calibr_left_y=args.calib_left_y,
        calibr_right_x=args.calib_right_x,
        calibr_right_y=args.calib_right_y,
        clear_rgb=tuple(args.clear_rgb),
        print_every=args.print_every,
    )

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
            clear_r=0.02,
            clear_g=0.02,
            clear_b=0.02,
        )
        save_full_config(cal, comf)
        print(f"[INFO] Saved to {_default_config_path()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
