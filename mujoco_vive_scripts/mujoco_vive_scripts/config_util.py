from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Calibration:
    left_x: int = 0
    left_y: int = 0
    right_x: int = 0
    right_y: int = 0


def _default_config_path() -> Path:
    if env := os.environ.get("MUJOCO_VIVE_CALIBRATION"):
        return Path(env)
    return Path(__file__).resolve().parent.parent / "calibration.toml"


def load_calibration(path: str | Path | None = None) -> Calibration | None:
    p = Path(path) if path else _default_config_path()
    if not p.exists():
        return None
    data = tomllib.loads(p.read_text())
    c = data.get("calibration", {})
    return Calibration(
        left_x=c.get("left_x", 0),
        left_y=c.get("left_y", 0),
        right_x=c.get("right_x", 0),
        right_y=c.get("right_y", 0),
    )


def save_calibration(
    cal: Calibration,
    path: str | Path | None = None,
) -> None:
    p = Path(path) if path else _default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "[calibration]\n"
        f"left_x = {cal.left_x}\n"
        f"left_y = {cal.left_y}\n"
        f"right_x = {cal.right_x}\n"
        f"right_y = {cal.right_y}\n"
    )
