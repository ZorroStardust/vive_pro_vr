import argparse, os
from pathlib import Path
os.environ.setdefault("MUJOCO_GL", "egl")
import numpy as np
from PIL import Image
import mujoco

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--left-camera", default="endo_left")
    p.add_argument("--right-camera", default="endo_right")
    p.add_argument("--output-dir", default="out")
    p.add_argument("--eye-w", type=int, default=720)
    p.add_argument("--eye-h", type=int, default=800)
    p.add_argument("--steps", type=int, default=120)
    p.add_argument("--swap-eyes", action="store_true")
    args = p.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    model = mujoco.MjModel.from_xml_path(args.model)
    data = mujoco.MjData(model)
    for _ in range(args.steps):
        mujoco.mj_step(model, data)
    renderer = mujoco.Renderer(model, height=args.eye_h, width=args.eye_w)
    try:
        renderer.update_scene(data, camera=args.left_camera)
        left = renderer.render()
        renderer.update_scene(data, camera=args.right_camera)
        right = renderer.render()
        if args.swap_eyes:
            left, right = right, left
        sbs = np.concatenate([left, right], axis=1)
        Image.fromarray(left).save(out / "left.png")
        Image.fromarray(right).save(out / "right.png")
        Image.fromarray(sbs).save(out / "sbs.png")
        print("[OK] Saved:", out / "left.png", out / "right.png", out / "sbs.png")
    finally:
        renderer.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
