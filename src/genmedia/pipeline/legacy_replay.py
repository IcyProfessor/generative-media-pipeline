"""Pre-v0.2 MP3/SRT/JSON converter. Preserve old timing, never claim verification."""
from pathlib import PureWindowsPath
import math
from ..assets.manifest import Catalog
from ..errors import QualityError
from ..models import Scene, ScenePlan, Box, TextLayer, ImageLayer, Motion
from .validation import validate_plan


def convert_plan(data: list, catalog: Catalog, labels: list[str], duration: float,
                 max_tail: float = 5.0) -> tuple[ScenePlan, float]:
    if not math.isfinite(max_tail) or max_tail < 0 or not math.isfinite(duration) or duration <= 0:
        raise QualityError("Replay duration and tail tolerance must be finite and nonnegative")
    if not isinstance(data, list) or not data:
        raise QualityError("Legacy scene JSON must be a non-empty list")
    def resolve_id(raw: str, kind: str):
        if not raw:
            return None
        path = PureWindowsPath(raw).as_posix().lower()
        matches = [a.id for a in catalog.by_id.values() if a.kind == kind and
            (path == a.path.replace("\\", "/").lower() or path.endswith("/"+a.path.replace("\\", "/").lower()))]
        if len(matches) != 1:
            raise QualityError(f"Legacy {kind} path has no unique manifest mapping")
        return matches[0]
    scenes, cursor = [], 0.0
    try:
        for i, item in enumerate(data):
            value = item["duration"]
            if isinstance(value, bool):
                raise QualityError("Boolean legacy duration")
            length = float(value)
            if not math.isfinite(length) or length <= 0:
                raise QualityError("Invalid legacy duration")
            label = str(item["letter"]).upper()
            asset_id = resolve_id(item.get("animal", ""), "character")
            color = item.get("letter_color") or "#243750"
            layers = []
            if i == 0 or i == len(data)-1:
                layers.append(TextLayer(text="Let's learn!" if i == 0 else "Well done!", color=color,
                                         box=Box(x=.06, y=.06, width=.88, height=.17), font_size=.09))
            else:
                layers.append(TextLayer(text=label, color=color, font_size=.36,
                    box=Box(x=.08, y=.18, width=.35, height=.45), motion=Motion(pulse=.045)))
            if asset_id:
                layers.append(ImageLayer(asset_id=asset_id, box=Box(x=.53, y=.15, width=.4, height=.55)))
            scenes.append(Scene(id=label, start=cursor, end=cursor+length,
                                background_id=resolve_id(item["background"], "background"), layers=layers))
            cursor += length
    except (ValueError, KeyError, TypeError) as exc:
        raise QualityError("Invalid legacy scene data") from exc
    if [x.id for x in scenes] != ["INTRO", *labels, "OUTRO"]:
        raise QualityError("Legacy scenes do not contain the configured alphabet in order")
    adjustment = duration-cursor
    if adjustment < -.1 or adjustment > max_tail:
        raise QualityError(f"Legacy plan/audio duration difference {adjustment:.3f}s exceeds replay tolerance")
    if duration <= scenes[-1].start:
        raise QualityError("Legacy outro cannot fit within the audio")
    scenes[-1].end = duration
    plan = ScenePlan(duration=duration, scenes=scenes)
    validate_plan(plan, catalog)
    return plan, adjustment
