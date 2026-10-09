"""Recipe-blind ScenePlan v2 backend: RGBA compositing + MoviePy/FFmpeg.

No script interpretation, label recognition, number replication or API calls.
All layout/meaning has already been compiled by the recipe into bounded layers.
"""
from __future__ import annotations
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
import math
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from moviepy import VideoClip, AudioFileClip
from ..assets.manifest import Catalog
from ..models import ScenePlan, RenderConfig, Span, TextLayer, ImageLayer
from ..pipeline.validation import validate_plan
from ..media import audio_duration, verify_video
from ..errors import QualityError

# Retained resized-frame/background budgets, NOT a process-wide RSS guarantee.
MAX_CACHE_BYTES = 256 * 1024 * 1024
ALLOWED_IMAGE_FORMATS = ("GIF", "PNG", "JPEG", "WEBP")

class Sprite:
    FRAME_CACHE_BYTES = 32 * 1024 * 1024

    def __init__(self, path: Path, box: tuple[int, int]):
        self.path = path
        self.box = box
        self.ends = []

        self._cache = {}
        self._cache_sizes = {}
        self._cache_bytes = 0

        self._source = Image.open(path, formats=ALLOWED_IMAGE_FORMATS)

        if self._source.width * self._source.height > 32_000_000:
            self._source.close()
            raise QualityError("Sprite source exceeds pixel safety limit")

        frame_count = getattr(self._source, "n_frames", 1)

        if frame_count > 1000:
            self._source.close()
            raise QualityError("GIF exceeds the 1000-frame safety limit")

        elapsed = 0.0

        try:
            for index in range(frame_count):
                self._source.seek(index)

                duration = self._source.info.get("duration", 100)

                if (
                    not isinstance(duration, (int, float))
                    or not math.isfinite(duration)
                ):
                    raise QualityError(
                        "Invalid animated image frame duration"
                    )

                elapsed += max(0.01, duration / 1000)
                self.ends.append(elapsed)

            self._source.seek(0)

        except Exception:
            self._source.close()
            raise

        if not self.ends:
            self._source.close()
            raise QualityError("Sprite contains no frames")

        self.duration = elapsed

    @property
    def cache_limit(self) -> int:
        return self.FRAME_CACHE_BYTES

    def _decode(self, index: int) -> Image.Image:
        if index in self._cache:
            image = self._cache.pop(index)
            size = self._cache_sizes.pop(index)

            self._cache[index] = image
            self._cache_sizes[index] = size

            return image

        self._source.seek(index)

        image = self._source.convert("RGBA")

        target_width, target_height = self.box

        if image.width <= 0 or image.height <= 0:
            raise QualityError("Sprite frame has invalid dimensions")

        # Unlike PIL.Image.thumbnail(), this deliberately allows upscaling.
        # ScenePlan's box describes the intended on-screen visual size, not
        # merely a maximum size for high-resolution source images.
        scale = min(
            target_width / image.width,
            target_height / image.height,
        )

        new_width = max(1, round(image.width * scale))
        new_height = max(1, round(image.height * scale))

        if (new_width, new_height) != image.size:
            image = image.resize(
                (new_width, new_height),
                Image.Resampling.LANCZOS,
            )

        size = image.width * image.height * 4

        # A single unusually large frame may be rendered, but is not retained.
        if size > self.FRAME_CACHE_BYTES:
            return image

        while (
            self._cache
            and self._cache_bytes + size > self.FRAME_CACHE_BYTES
        ):
            oldest = next(iter(self._cache))

            self._cache.pop(oldest)
            self._cache_bytes -= self._cache_sizes.pop(oldest)

        self._cache[index] = image
        self._cache_sizes[index] = size
        self._cache_bytes += size

        return image

    def frame(self, t: float) -> Image.Image:
        index = min(
            bisect_right(
                self.ends,
                t % self.duration,
            ),
            len(self.ends) - 1,
        )

        return self._decode(index)

    def close(self):
        self._cache.clear()
        self._cache_sizes.clear()
        self._cache_bytes = 0

        source = getattr(self, "_source", None)

        if source is not None:
            source.close()
            self._source = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

class Canvas:
    def __init__(
        self,
        plan: ScenePlan,
        catalog: Catalog,
        cues: list[Span],
        cfg: RenderConfig,
    ):
        self.plan = plan
        self.catalog = catalog
        self.cues = cues
        self.cfg = cfg

        self.starts = [x.start for x in plan.scenes]
        self.cue_starts = [x.start for x in cues]
        self.size = (cfg.width, cfg.height)

        # Decoded media is loaded lazily. Full A-Z 1080p plans can contain
        # enough GIF frames/backgrounds to exceed hundreds of MiB if the
        # entire catalog subset is decoded up front.
        self.backgrounds = {}
        self.background_bytes = {}
        self.sprites = {}
        self.sprite_bytes = {}
        self.cache_bytes = 0

        self.font = lru_cache(maxsize=16)(self._font)
        self.text_image = lru_cache(maxsize=64)(self._text_image)

    def close(self):
        for sprite in self.sprites.values():
            sprite.close()
        for background in self.backgrounds.values():
            background.close()
        self.sprites.clear()
        self.sprite_bytes.clear()
        self.backgrounds.clear()
        self.background_bytes.clear()
        self.cache_bytes = 0
        self.text_image.cache_clear()
        self.font.cache_clear()

    def sprite_key(self, layer):
        return (
            layer.asset_id,
            max(1, round(layer.box.width * self.cfg.width)),
            max(1, round(layer.box.height * self.cfg.height)),
        )

    def _evict_until(self, required: int):
        if required > MAX_CACHE_BYTES:
            raise QualityError(
                "Single decoded render asset exceeds 256 MiB"
            )

        while self.cache_bytes + required > MAX_CACHE_BYTES:
            if self.sprites:
                key = next(iter(self.sprites))

                sprite = self.sprites.pop(key)
                sprite.close()

                self.cache_bytes -= self.sprite_bytes.pop(key)
                continue

            if self.backgrounds:
                key = next(iter(self.backgrounds))

                self.backgrounds.pop(key)
                self.cache_bytes -= self.background_bytes.pop(key)
                continue

            raise QualityError(
                "Decoded canvas cache cannot fit render asset"
            )

    def _background(self, asset_id):
        if asset_id in self.backgrounds:
            image = self.backgrounds.pop(asset_id)
            size = self.background_bytes.pop(asset_id)

            # Reinsert to make the normal dict behave as a tiny LRU cache.
            self.backgrounds[asset_id] = image
            self.background_bytes[asset_id] = size
            return image

        with Image.open(self.catalog.resolve(asset_id), formats=ALLOWED_IMAGE_FORMATS) as bg:
            if bg.width * bg.height > 32_000_000:
                raise QualityError(
                    "Background source exceeds pixel safety limit"
                )

            image = ImageOps.fit(
                bg.convert("RGBA"),
                self.size,
            )

        required = image.width * image.height * 4
        self._evict_until(required)

        self.backgrounds[asset_id] = image
        self.background_bytes[asset_id] = required
        self.cache_bytes += required

        return image

    def _sprite(self, layer):
        key = self.sprite_key(layer)

        if key in self.sprites:
            sprite = self.sprites.pop(key)
            reserved = self.sprite_bytes.pop(key)

            self.sprites[key] = sprite
            self.sprite_bytes[key] = reserved

            return sprite

        sprite = Sprite(
            self.catalog.resolve(layer.asset_id),
            key[1:],
        )

        # Reserve the sprite's bounded frame-cache budget instead of
        # decoding and accounting for the whole animation up front.
        required = sprite.cache_limit

        try:
            self._evict_until(required)
        except Exception:
            sprite.close()
            raise

        self.sprites[key] = sprite
        self.sprite_bytes[key] = required
        self.cache_bytes += required

        return sprite

    def _font(self, size: int):
        return (
            ImageFont.truetype(self.cfg.font_path, size)
            if self.cfg.font_path
            else ImageFont.load_default(size=size)
        )

    def _text_image(
        self,
        text: str,
        size: int,
        color: str,
        max_width: int,
        max_height: int,
    ) -> Image.Image:
        font = self.font(max(1, size))
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        lines = []

        for paragraph in text.splitlines() or [text]:
            line = ""

            for word in paragraph.split():
                trial = (line + " " + word).strip()

                if (
                    line
                    and probe.textbbox(
                        (0, 0),
                        trial,
                        font=font,
                    )[2] > max_width - 12
                ):
                    lines.append(line)
                    line = word
                else:
                    line = trial

            if line:
                lines.append(line)

        rendered_text = "\n".join(lines) or " "

        box = probe.multiline_textbbox(
            (0, 0),
            rendered_text,
            font=font,
            spacing=4,
            stroke_width=1,
        )

        image = Image.new(
            "RGBA",
            (
                max(1, box[2] - box[0] + 12),
                max(1, box[3] - box[1] + 12),
            ),
        )

        ImageDraw.Draw(image).multiline_text(
            (6 - box[0], 6 - box[1]),
            rendered_text,
            font=font,
            fill=color,
            stroke_width=1,
            stroke_fill="#FFFFFF",
            spacing=4,
            align="center",
        )

        image.thumbnail(
            (max_width, max_height),
            Image.Resampling.LANCZOS,
        )

        return image

    @staticmethod
    def paste(canvas, image, xy, alpha=1.0):
        if alpha < 1:
            image = image.copy()
            image.putalpha(
                image.getchannel("A").point(
                    lambda x: int(x * max(0, alpha))
                )
            )

        canvas.alpha_composite(
            image,
            (int(xy[0]), int(xy[1])),
        )

    def frame(self, t: float):
        index = max(
            0,
            min(
                bisect_right(self.starts, t) - 1,
                len(self.plan.scenes) - 1,
            ),
        )

        scene = self.plan.scenes[index]
        local = max(0, t - scene.start)
        w, h = self.size

        canvas = self._background(
            scene.background_id
        ).copy()

        for layer in scene.layers:
            box = layer.box
            motion = layer.motion

            if isinstance(layer, ImageLayer):
                image = self._sprite(layer).frame(local)

            elif isinstance(layer, TextLayer):
                image = self.text_image(
                    layer.text,
                    max(1, int(h * layer.font_size)),
                    layer.color,
                    max(1, int(w * box.width)),
                    max(1, int(h * box.height)),
                )

            else:
                raise QualityError(
                    "Unsupported scene layer"
                )

            if motion.pulse:
                scale = (
                    1
                    + motion.pulse
                    * math.sin(local * 2 * math.pi)
                )

                image = image.resize(
                    (
                        max(1, round(image.width * scale)),
                        max(1, round(image.height * scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )

            x = (
                (box.x + box.width / 2) * w
                - image.width / 2
            )

            y = (
                (box.y + box.height / 2) * h
                - image.height / 2
            )

            progress = min(
                1,
                local / motion.enter_seconds,
            )

            ease = 1 - (1 - progress) ** 3

            if motion.enter == "left":
                x = (
                    -image.width
                    + (x + image.width) * ease
                )

            elif motion.enter == "right":
                x = w + (x - w) * ease

            elif motion.enter == "top":
                y = (
                    -image.height
                    + (y + image.height) * ease
                )

            elif motion.enter == "bottom":
                y = h + (y - h) * ease

            y += (
                h
                * motion.bob
                * math.sin(local * 2 * math.pi)
            )

            fade_time = min(
                motion.fade_seconds,
                scene.duration / 3,
            )

            fade = (
                min(
                    1,
                    local / fade_time,
                    max(0, scene.end - t) / fade_time,
                )
                if fade_time
                else 1
            )

            self.paste(
                canvas,
                image,
                (x, y),
                fade,
            )

        cue_index = (
            bisect_right(self.cue_starts, t) - 1
        )

        if cue_index >= 0:
            cue = self.cues[cue_index]

            if t < cue.end:
                caption = self.text_image(
                    cue.text,
                    max(12, int(h * 0.05)),
                    "#16243A",
                    int(w * 0.9),
                    int(h * 0.19),
                )

                x = (w - caption.width) / 2
                y = h - caption.height - 12

                panel = Image.new(
                    "RGBA",
                    (
                        min(w, caption.width + 20),
                        caption.height + 8,
                    ),
                    (255, 255, 255, 220),
                )

                self.paste(
                    canvas,
                    panel,
                    (max(0, x - 10), y - 4),
                )

                self.paste(
                    canvas,
                    caption,
                    (x, y),
                )

        return np.asarray(
            canvas.convert("RGB")
        )

def render(plan: ScenePlan, catalog: Catalog, cues: list[Span], audio_path: Path,
           output_path: Path, cfg: RenderConfig) -> None:
    validate_plan(plan, catalog)
    if abs(audio_duration(audio_path)-plan.duration) > .15:
        raise QualityError("Audio duration differs from the stored scene plan")
    if any(b.start < a.start or b.start < a.end-.02 for a, b in zip(cues, cues[1:])):
        raise QualityError("Subtitle cues are out of order or overlap")
    if any(cue.end > plan.duration+.1 for cue in cues):
        raise QualityError("Subtitles extend beyond audio")
    canvas = Canvas(plan, catalog, cues, cfg)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_name(output_path.stem+".part.mp4")
    try:
        with TemporaryDirectory(prefix="render-", dir=output_path.parent) as temp:
            with AudioFileClip(str(audio_path)) as audio:
                with VideoClip(frame_function=canvas.frame, duration=plan.duration) as video:
                    final = video.with_audio(audio)
                    try:
                        final.write_videofile(str(partial), fps=cfg.fps, codec=cfg.codec,
                            audio_codec="aac", audio_bitrate="128k", threads=cfg.threads,
                            preset=cfg.preset, pixel_format="yuv420p", logger="bar",
                            temp_audiofile=str(Path(temp)/"audio.m4a"))
                    finally:
                        final.close()
        verify_video(partial, timeout=cfg.timeout_seconds)
        os.replace(partial, output_path)
    finally:
        canvas.close()
        partial.unlink(missing_ok=True)
