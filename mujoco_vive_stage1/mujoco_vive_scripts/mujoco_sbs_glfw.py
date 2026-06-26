"""
Python MuJoCo stereo rendering test — NOT an OpenXR compositor submission program.

Renders two MuJoCo fixed cameras side-by-side using GLFW/OpenGL in a regular
window or on a regular monitor. If the VIVE is currently owned by Monado
NVIDIA Direct-Mode, GLFW windows will NOT appear on the HMD.
"""

import argparse, math, time, sys
import mujoco
from mujoco.glfw import glfw

def camera_id(model, name):
    cid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, name)
    if cid < 0:
        raise RuntimeError(f"Camera not found: {name}")
    return cid

def render(model, data, scene, context, option, cam, cid, viewport):
    cam.type = mujoco.mjtCamera.mjCAMERA_FIXED
    cam.fixedcamid = cid
    mujoco.mjv_updateScene(model, data, option, None, cam, mujoco.mjtCatBit.mjCAT_ALL, scene)
    mujoco.mjr_render(viewport, scene, context)

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--left-camera", default="endo_left")
    p.add_argument("--right-camera", default="endo_right")
    p.add_argument("--monitor", type=int, default=0)
    p.add_argument("--fullscreen", action="store_true")
    p.add_argument("--windowed", action="store_true")
    p.add_argument("--width", type=int, default=1440)
    p.add_argument("--height", type=int, default=800)
    p.add_argument("--fps", type=float, default=60)
    p.add_argument("--swap-eyes", action="store_true")
    p.add_argument("--no-vsync", action="store_true")
    args = p.parse_args()

    if not glfw.init():
        print("[ERROR] glfw.init failed", file=sys.stderr)
        return 1
    window = None
    mjr_context = None
    try:
        monitors = glfw.get_monitors()
        for i, m in enumerate(monitors):
            mode = glfw.get_video_mode(m)
            print(f"[{i}] {glfw.get_monitor_name(m)}: {mode.size.width}x{mode.size.height}@{mode.refresh_rate}")
        if args.fullscreen:
            mon = monitors[args.monitor]
            mode = glfw.get_video_mode(mon)
            window = glfw.create_window(mode.size.width, mode.size.height, "MuJoCo SBS", mon, None)
        else:
            window = glfw.create_window(args.width, args.height, "MuJoCo SBS", None, None)
        if not window:
            raise RuntimeError("create_window failed")
        glfw.make_context_current(window)
        glfw.swap_interval(0 if args.no_vsync else 1)

        model = mujoco.MjModel.from_xml_path(args.model)
        data = mujoco.MjData(model)
        left, right = camera_id(model, args.left_camera), camera_id(model, args.right_camera)
        option = mujoco.MjvOption()
        scene = mujoco.MjvScene(model, maxgeom=10000)
        mjr_context = mujoco.MjrContext(model, mujoco.mjtFontScale.mjFONTSCALE_150)
        cam = mujoco.MjvCamera()
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "spin")
        spin_adr = int(model.jnt_qposadr[jid]) if jid >= 0 else None
        start, last, frames = time.perf_counter(), time.perf_counter(), 0
        while not glfw.window_should_close(window):
            if glfw.get_key(window, glfw.KEY_ESCAPE) == glfw.PRESS or glfw.get_key(window, glfw.KEY_Q) == glfw.PRESS:
                break
            t = time.perf_counter() - start
            if spin_adr is not None:
                data.qpos[spin_adr] = 0.9 * math.sin(1.25 * t)
                mujoco.mj_forward(model, data)
            else:
                mujoco.mj_step(model, data)
            fbw, fbh = glfw.get_framebuffer_size(window)
            half = fbw // 2
            full = mujoco.MjrRect(0, 0, fbw, fbh)
            lv = mujoco.MjrRect(0, 0, half, fbh)
            rv = mujoco.MjrRect(half, 0, fbw - half, fbh)
            mujoco.mjr_rectangle(full, 0, 0, 0, 1)
            if args.swap_eyes:
                render(model, data, scene, mjr_context, option, cam, right, lv)
                render(model, data, scene, mjr_context, option, cam, left, rv)
            else:
                render(model, data, scene, mjr_context, option, cam, left, lv)
                render(model, data, scene, mjr_context, option, cam, right, rv)
            glfw.swap_buffers(window)
            glfw.poll_events()
            frames += 1
            now = time.perf_counter()
            if now - last > 2:
                print(f"FPS: {frames/(now-last):.1f} framebuffer={fbw}x{fbh}")
                frames, last = 0, now
            if args.fps > 0:
                time.sleep(max(0, 1/args.fps - (time.perf_counter()-now)))
    finally:
        if mjr_context is not None:
            mjr_context.free()
        if window is not None:
            glfw.destroy_window(window)
        glfw.terminate()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
