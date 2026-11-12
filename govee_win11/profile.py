"""Measured device parameters (see data/device_profile.json)."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal

from .paths import resource

DEFAULT_PATH = resource("data", "device_profile.json")


@dataclass
class DeviceProfile:
    segment_count: int = 15
    bit_order: Literal["start_at_plug", "start_at_end"] = "start_at_plug"
    brightness_max: int = 255
    max_packets_per_s: int = 20
    scene_cmd_version: int = 0

    @property
    def full_mask(self) -> int:
        return (1 << self.segment_count) - 1


def load_profile(path: Path | None = None) -> DeviceProfile:
    data = json.loads((path or DEFAULT_PATH).read_text(encoding="utf-8"))
    names = {f.name for f in fields(DeviceProfile)}
    return DeviceProfile(**{k: v for k, v in data.items() if k in names})
