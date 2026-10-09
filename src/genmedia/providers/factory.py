"""Composition root. Core stages only depend on the ports in base.py."""
from ..errors import PipelineError


def validate_selection(cfg):
    for kind, choices in {"text": {"fixture", "gemini"}, "audio": {"fixture", "suno"},
                          "alignment": {"fixture", "suno"}, "renderer": {"moviepy"}}.items():
        if getattr(cfg.providers, kind) not in choices:
            raise PipelineError(f"Unknown {kind} provider; register it in the composition root")
    if cfg.providers.audio != "fixture" and cfg.providers.alignment == "fixture":
        raise PipelineError("Synthetic alignment cannot be used as evidence for real audio")
    if cfg.providers.audio == "fixture" and cfg.providers.alignment == "suno":
        raise PipelineError("Suno alignment requires an actual Suno task/candidate")


def text_provider(cfg, recipe, catalog):
    if cfg.providers.text == "fixture":
        from .fixture import FixtureText
        return FixtureText(recipe, catalog, cfg.seed)
    if cfg.providers.text == "gemini":
        from .gemini import GeminiText
        return GeminiText(cfg.gemini.model)
    raise PipelineError("Text provider is not registered")


def audio_provider(cfg, script):
    if cfg.providers.audio == "fixture":
        from .fixture import FixtureAudio
        return FixtureAudio(script)
    if cfg.providers.audio == "suno":
        from .suno import SunoAudio
        return SunoAudio(cfg.suno)
    raise PipelineError("Audio provider is not registered")


def alignment_provider(cfg):
    if cfg.providers.alignment == "fixture":
        from .fixture import FixtureAlignment
        return FixtureAlignment()
    if cfg.providers.alignment == "suno":
        from .suno import SunoAlignment
        return SunoAlignment(cfg.suno)
    raise PipelineError("Alignment provider is not registered")
