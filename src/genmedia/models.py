"""Versioned, provider- and recipe-neutral contracts.

Recipes own the meaning of script segment IDs. The renderer only knows bounded
text/image layers; it never interprets IDs as letters, numbers or section names.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Color = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
Unit = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Asset(Model):
    id: Identifier
    kind: Literal["character", "background"]
    path: str
    label: str | None = None
    object: str = ""
    plural: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    plural_aliases: list[str] = Field(default_factory=list)
    provenance: str = "User supplied; redistribution permissions not verified"


class Manifest(Model):
    version: Literal[1] = 1
    root: str = "."
    assets: list[Asset]

    @model_validator(mode="after")
    def unique(self):
        ids = [a.id for a in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate asset IDs")
        return self


class Segment(Model):
    id: Identifier
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    asset_id: Identifier | None = None
    # Audio backends may use the hint, but the core assigns it no semantics.
    audio_section: Literal["intro", "verse", "chorus", "outro", "speech"] = "speech"


class Script(Model):
    version: Literal[1] = 1
    title: Annotated[str, Field(min_length=1, max_length=80)]
    segments: list[Segment] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique(self):
        ids = [s.id for s in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate script segment IDs")
        return self

    def plain_text(self) -> str:
        return "\n".join(s.text for s in self.segments)


class Span(Model):
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    start: Seconds
    end: PositiveSeconds

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("Span must have positive duration")
        return self


class Transcript(Model):
    version: Literal[1] = 1
    provider: Identifier
    granularity: Literal["word", "segment"] = "word"
    spans: list[Span] = Field(min_length=1, max_length=50000)
    # Deliberately no arbitrary raw provider response or URL in this contract.
    acoustic_verification: Literal["not_performed"] = "not_performed"


class Timing(Model):
    id: Identifier
    start: Seconds
    end: PositiveSeconds

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("Scene must have positive duration")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start


class Visual(Model):
    id: Identifier
    asset_id: Identifier | None = None
    background_id: Identifier
    text_color: Color = "#243750"


class VisualPlan(Model):
    scenes: list[Visual] = Field(min_length=1, max_length=256)


class Box(Model):
    """Normalized rectangle. Layers are contained in it before animation."""
    x: Unit
    y: Unit
    width: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
    height: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]

    @model_validator(mode="after")
    def inside_canvas(self):
        if self.x + self.width > 1.0000001 or self.y + self.height > 1.0000001:
            raise ValueError("Layer box must fit inside normalized canvas")
        return self


class Motion(Model):
    fade_seconds: Annotated[float, Field(ge=0, le=5, allow_inf_nan=False)] = 0.25
    enter: Literal["none", "left", "right", "top", "bottom"] = "none"
    enter_seconds: Annotated[float, Field(gt=0, le=5, allow_inf_nan=False)] = 0.6
    pulse: Annotated[float, Field(ge=0, le=0.15, allow_inf_nan=False)] = 0
    bob: Annotated[float, Field(ge=0, le=0.05, allow_inf_nan=False)] = 0


class TextLayer(Model):
    type: Literal["text"] = "text"
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    box: Box
    color: Color = "#243750"
    font_size: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)] = 0.12
    motion: Motion = Field(default_factory=Motion)


class ImageLayer(Model):
    type: Literal["image"] = "image"
    asset_id: Identifier
    box: Box
    motion: Motion = Field(default_factory=Motion)


Layer = Annotated[TextLayer | ImageLayer, Field(discriminator="type")]


class Scene(Timing):
    background_id: Identifier
    layers: list[Layer] = Field(default_factory=list, max_length=64)


class ScenePlan(Model):
    version: Literal[2] = 2
    duration: PositiveSeconds
    scenes: list[Scene] = Field(min_length=1, max_length=256)


class Candidate(Model):
    id: Annotated[str, Field(min_length=1, max_length=200)]
    url: Annotated[str, Field(min_length=1, max_length=8192)]
    extension: Literal["mp3", "wav"] = "mp3"


class RecipeSpec(Model):
    name: Identifier = "alphabet"
    options: dict[str, Any] = Field(default_factory=dict)


class ProviderSpec(Model):
    text: Identifier = "fixture"
    audio: Identifier = "fixture"
    alignment: Identifier = "fixture"
    renderer: Identifier = "moviepy"

    @property
    def needs_paid_ack(self) -> bool:
        # Unknown/custom providers are conservatively treated as potentially paid.
        return any(x != "fixture" for x in (self.text, self.audio, self.alignment))


class RenderConfig(Model):
    width: int = Field(default=1280, ge=160, le=3840)
    height: int = Field(default=720, ge=90, le=2160)
    fps: int = Field(default=30, ge=1, le=60)
    threads: int = Field(default=2, ge=1, le=32)
    codec: Literal["libx264", "h264_qsv"] = "libx264"
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"] = "medium"
    font_path: str | None = None
    timeout_seconds: int = Field(default=7200, ge=10, le=86400)
    serial: bool = True

    @model_validator(mode="after")
    def even_size(self):
        if self.width % 2 or self.height % 2:
            raise ValueError("Use even width and height for yuv420p")
        return self


class GeminiConfig(Model):
    # Explicitly configurable: account access and model lifecycle are not assumed.
    model: str = "gemini-3.5-flash-lite"


class SunoConfig(Model):
    model: str = "V5"
    base_url: str = "https://api.sunoapi.org/api/v1"
    callback_url: str | None = None
    request_format: Literal["prompt", "legacy_lyrics"] = "prompt"
    include_duration: bool = False


class AudioConfig(Model):
    style: str = "Cheerful educational song with clearly articulated vocals"
    duration_seconds: int = Field(default=45, ge=10, le=360)
    poll_interval_seconds: float = Field(default=30, gt=0, le=300)
    poll_timeout_seconds: float = Field(default=1200, gt=0, le=7200)


class Config(Model):
    version: Literal[2] = 2
    manifest: str
    output_dir: str = "output"
    recipe: RecipeSpec = Field(default_factory=RecipeSpec)
    providers: ProviderSpec = Field(default_factory=ProviderSpec)
    seed: int = 42
    audio: AudioConfig = Field(default_factory=AudioConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    suno: SunoConfig = Field(default_factory=SunoConfig)
