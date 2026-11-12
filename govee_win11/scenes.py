"""Govee scene library (loaded from the bundled scenes JSON)."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .paths import resource
from .protocol import scene_packets

DEFAULT_PATH = resource("data", "scenes_H6125.json")


@dataclass
class Scene:
    id: int
    name: str
    category: str
    hint: str
    effects: list[dict] = field(default_factory=list)
    code: int = 0


def _parse(path: Path) -> list[Scene]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))["data"]
    out: list[Scene] = []
    for cat in data["categories"]:
        for s in cat["scenes"]:
            out.append(Scene(
                id=s["sceneId"],
                name=s["sceneName"],
                category=cat["categoryName"],
                hint=s.get("scenesHint") or "",
                effects=list(s.get("lightEffects") or []),
                code=s.get("sceneCode") or 0,
            ))
    return out


@lru_cache(maxsize=1)
def _default() -> tuple[Scene, ...]:
    return tuple(_parse(DEFAULT_PATH))


def load_scenes(path: Path | None = None) -> list[Scene]:
    if path is None:
        return list(_default())
    return _parse(Path(path))


def categories(scenes: list[Scene]) -> list[str]:
    return list(dict.fromkeys(s.category for s in scenes))


def packets_for(scene: Scene, cmd_version: int) -> list[bytes]:
    if not scene.effects:
        return scene_packets(scene.code, b"")
    effect = next((e for e in scene.effects if e.get("cmdVersion") == cmd_version),
                  scene.effects[0])
    code = effect.get("sceneCode") or scene.code
    param = base64.b64decode(effect.get("scenceParam") or "")
    return scene_packets(code, param, int(effect.get("sceneType") or 2))
