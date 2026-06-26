from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Calibration:
    left_x: int = 0
    left_y: int = 0
    right_x: int = 0
    right_y: int = 0


@dataclass
class ComfortConfig:
    mono_to_both_eyes: bool = False
    swap_eyes: bool = False
    scene_farther_px: float = 0.0
    scene_shift_step_px: float = 2.0
    max_abs_scene_shift_px: float = 80.0
    invert_scene_shift: bool = False
    clear_r: float = 0.02
    clear_g: float = 0.02
    clear_b: float = 0.02

    def to_rgb_tuple(self) -> tuple[float, float, float]:
        return (self.clear_r, self.clear_g, self.clear_b)


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


def load_comfort(path: str | Path | None = None) -> ComfortConfig | None:
    p = Path(path) if path else _default_config_path()
    if not p.exists():
        return None
    data = tomllib.loads(p.read_text())
    c = data.get("comfort", {})
    if not c:
        return None
    return ComfortConfig(
        mono_to_both_eyes=c.get("mono_to_both_eyes", False),
        swap_eyes=c.get("swap_eyes", False),
        scene_farther_px=c.get("scene_farther_px", 0.0),
        scene_shift_step_px=c.get("scene_shift_step_px", 2.0),
        max_abs_scene_shift_px=c.get("max_abs_scene_shift_px", 80.0),
        invert_scene_shift=c.get("invert_scene_shift", False),
        clear_r=c.get("clear_r", 0.02),
        clear_g=c.get("clear_g", 0.02),
        clear_b=c.get("clear_b", 0.02),
    )


def _format_section(name: str, items: list[tuple[str, str | int | float | bool]]) -> str:
    lines = [f"[{name}]"]
    for key, val in items:
        if isinstance(val, bool):
            lines.append(f"{key} = {'true' if val else 'false'}")
        elif isinstance(val, float):
            lines.append(f"{key} = {val:g}")
        else:
            lines.append(f"{key} = {val}")
    return "\n".join(lines) + "\n"


def save_calibration(
    cal: Calibration,
    path: str | Path | None = None,
) -> None:
    p = Path(path) if path else _default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    comfort = load_comfort(p)

    cal_section = _format_section("calibration", [
        ("left_x", cal.left_x),
        ("left_y", cal.left_y),
        ("right_x", cal.right_x),
        ("right_y", cal.right_y),
    ])

    parts = [cal_section]
    if comfort is not None:
        parts.append(_format_section("comfort", [
            ("mono_to_both_eyes", comfort.mono_to_both_eyes),
            ("swap_eyes", comfort.swap_eyes),
            ("scene_farther_px", comfort.scene_farther_px),
            ("scene_shift_step_px", comfort.scene_shift_step_px),
            ("max_abs_scene_shift_px", comfort.max_abs_scene_shift_px),
            ("invert_scene_shift", comfort.invert_scene_shift),
            ("clear_r", comfort.clear_r),
            ("clear_g", comfort.clear_g),
            ("clear_b", comfort.clear_b),
        ]))

    p.write_text("\n".join(parts) + "\n")


def save_comfort(
    c: ComfortConfig,
    path: str | Path | None = None,
) -> None:
    p = Path(path) if path else _default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    cal = load_calibration(p)

    parts: list[str] = []
    if cal is not None:
        parts.append(_format_section("calibration", [
            ("left_x", cal.left_x),
            ("left_y", cal.left_y),
            ("right_x", cal.right_x),
            ("right_y", cal.right_y),
        ]))

    parts.append(_format_section("comfort", [
        ("mono_to_both_eyes", c.mono_to_both_eyes),
        ("swap_eyes", c.swap_eyes),
        ("scene_farther_px", c.scene_farther_px),
        ("scene_shift_step_px", c.scene_shift_step_px),
        ("max_abs_scene_shift_px", c.max_abs_scene_shift_px),
        ("invert_scene_shift", c.invert_scene_shift),
        ("clear_r", c.clear_r),
        ("clear_g", c.clear_g),
        ("clear_b", c.clear_b),
    ]))

    p.write_text("\n".join(parts) + "\n")


def save_full_config(
    cal: Calibration,
    comfort: ComfortConfig,
    path: str | Path | None = None,
) -> None:
    p = Path(path) if path else _default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    parts = [
        _format_section("calibration", [
            ("left_x", cal.left_x),
            ("left_y", cal.left_y),
            ("right_x", cal.right_x),
            ("right_y", cal.right_y),
        ]),
        _format_section("comfort", [
            ("mono_to_both_eyes", comfort.mono_to_both_eyes),
            ("swap_eyes", comfort.swap_eyes),
            ("scene_farther_px", comfort.scene_farther_px),
            ("scene_shift_step_px", comfort.scene_shift_step_px),
            ("max_abs_scene_shift_px", comfort.max_abs_scene_shift_px),
            ("invert_scene_shift", comfort.invert_scene_shift),
            ("clear_r", comfort.clear_r),
            ("clear_g", comfort.clear_g),
            ("clear_b", comfort.clear_b),
        ]),
    ]

    p.write_text("\n".join(parts) + "\n")
