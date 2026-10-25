"""Config loading never searches for .env or applies live settings to a demo."""
from importlib.resources import files
from pathlib import Path
import os
import yaml
from .models import Config, RecipeSpec, ProviderSpec, RenderConfig
from .recipes import get_recipe
from .providers.factory import validate_selection


def builtin_config(recipe="alphabet", output: Path | None = None) -> Config:
    cfg = Config(manifest=str(files("genmedia").joinpath("data/assets/demo/manifest.json")),
                 output_dir=str((output or Path("output")).resolve()),
                 recipe=RecipeSpec(name=recipe),
                 render=RenderConfig(width=640, height=360, fps=12, preset="veryfast"))
    get_recipe(cfg.recipe)
    return cfg


def apply_live_env(cfg: Config) -> Config:

    for env, obj, key in (
        ("GEMINI_MODEL", cfg.gemini, "model"),
        ("SUNO_MODEL", cfg.suno, "model"),
        ("SUNO_BASE_URL", cfg.suno, "base_url"),
        ("SUNO_CALLBACK_URL", cfg.suno, "callback_url"),
    ):
        if os.environ.get(env):
            setattr(obj, key, os.environ[env])
    return cfg


def load_config(path: Path, *, live=False, output: Path | None = None) -> Config:
    path = path.resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a YAML mapping")
    if "labels" in data or "provider" in data:
        raise ValueError(
            "Unsupported v0.2 config: expected recipe.options and providers."
        )    
    cfg = Config.model_validate(data)
    cfg.manifest = str((path.parent / cfg.manifest).resolve())
    cfg.output_dir = str(output.resolve() if output else (path.parent / cfg.output_dir).resolve())
    if cfg.render.font_path:
        cfg.render.font_path = str((path.parent / cfg.render.font_path).resolve())
    if live:
        cfg.providers = ProviderSpec(text="gemini", audio="suno", alignment="suno")
    if cfg.providers.needs_paid_ack:
        apply_live_env(cfg)
    get_recipe(cfg.recipe)
    validate_selection(cfg)
    return cfg
