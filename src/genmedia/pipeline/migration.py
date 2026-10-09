"""Explicit v0.2 -> v0.3 conversion into a NEW, replay-only job.

Old outputs are never modified. Old MP4s/status/receipts are not accepted as
verification for the new renderer. Only saved source artifacts are imported.
"""
from pathlib import Path
import shutil
from ..models import (Config, RecipeSpec, ProviderSpec, AudioConfig, RenderConfig,
                      GeminiConfig, SunoConfig, Script, Segment, VisualPlan, Visual, Candidate)
from ..assets.manifest import Catalog
from ..providers.suno_schema import normalize_suno
from ..store import read_json, write_json, write_text
from ..errors import PipelineError
from .orchestrator import create_job, digest
from .. import __version__


def convert_script(data: dict) -> Script:
    return Script(title=data["title"], segments=[
        Segment(id="INTRO", text=data["intro"], audio_section="intro"),
        *[Segment(id=v["label"], text=v["text"], asset_id=v["asset_id"], audio_section="verse") for v in data["verses"]],
        Segment(id="OUTRO", text=data["outro"], audio_section="outro")])


def convert_visuals(data: dict) -> VisualPlan:
    return VisualPlan(scenes=[Visual(id=v["label"], asset_id=v.get("asset_id"),
                                    background_id=v["background_id"], text_color=v.get("text_color", "#243750"))
                             for v in data["scenes"]])


def migrate_v2(source: Path, output: Path, manifest: Path | None = None):
    source = source.resolve()
    old = read_json(source / "config.json")
    state = read_json(source / "job.json")
    if state.get("version") != 1 or old.get("version", 1) != 1:
        raise PipelineError("migrate-v2 expects a v0.2 job with state version 1")
    manifest_path = manifest.resolve() if manifest else Path(old["manifest"])
    if not manifest_path.is_absolute():
        manifest_path = (source/manifest_path).resolve()
    if not manifest_path.is_file():
        raise PipelineError("Old manifest path is unavailable; supply --manifest with a current local catalog")
    legacy_live = old.get("provider") == "live"
    cfg = Config(manifest=str(manifest_path), output_dir=str(output.resolve()),
        recipe=RecipeSpec(name="alphabet", options={"labels": old.get("labels", list("ABCDEF"))}),
        providers=ProviderSpec(text="gemini", audio="suno", alignment="suno") if legacy_live else ProviderSpec(),
        seed=old.get("seed", 42), render=RenderConfig.model_validate(old.get("render", {})),
        audio=AudioConfig(style=old.get("style", "Cheerful educational song"),
            duration_seconds=old.get("audio_duration_seconds", 45),
            poll_interval_seconds=old.get("poll_interval_seconds", 30),
            poll_timeout_seconds=old.get("poll_timeout_seconds", 1200)),
        gemini=GeminiConfig(model=old.get("gemini_model", "gemini-2.5-flash")),
        suno=SunoConfig(model=old.get("suno_model", "V6"), base_url=old.get("suno_base_url", "https://api.sunoapi.org/api/v1"), request_format="legacy_lyrics" if old.get("suno_model", "V6") in {"V6", "V5_5"} else "prompt"))
    script = convert_script(read_json(source/"lyrics.json"))
    from ..recipes import get_recipe
    get_recipe(cfg.recipe).validate_script(script, Catalog(manifest_path))
    candidates = [Candidate.model_validate(x) for x in state.get("candidates", [])]
    if not candidates:
        raise PipelineError("No saved candidates to migrate; keep the original v0.2 job")
    store = create_job(cfg)
    write_json(store.path/"script.json", script.model_dump())
    write_text(store.path/"script.txt", script.plain_text())
    # Discard old signed download URLs and task identifiers. Resume is prohibited.
    imported = [Candidate(id=f"imported-{i}", url=f"offline://{i}", extension=c.extension)
                for i, c in enumerate(candidates)]
    with store.lock():
        store.update(status="processing_variants", offline_only=True, task_id="imported-v2",
                     candidates=[c.model_dump() for c in imported], script_sha256=digest(store.path/"script.json"))
    missing = []
    for i, candidate in enumerate(candidates):
        branch = store.branch(i)
        old_branch = source/f"v{i}"
        name = f"audio.{candidate.extension}"
        for filename in (name,):
            if (old_branch/filename).is_file():
                shutil.copyfile(old_branch/filename, branch/filename)
            else:
                missing.append(f"v{i}/{filename}")
        raw_path = old_branch/"timestamped_words.json"
        if raw_path.exists():
            transcript = normalize_suno(read_json(raw_path))
            if not legacy_live:
                transcript.provider = "fixture"
            write_json(branch/"transcript.json", transcript.model_dump())
        else:
            missing.append(f"v{i}/timestamped_words.json")
        visual_path = old_branch/"visuals.json"
        if visual_path.exists():
            write_json(branch/"visuals.json", convert_visuals(read_json(visual_path)).model_dump())
        else:
            missing.append(f"v{i}/visuals.json")
        with store.lock(i):
            store.update(branch=i, status="pending", candidate_id=imported[i].id, audio_filename=name)
    write_json(store.path/"migration.json", {"from": "0.2", "to": __version__, "mode": "offline_only",
        "source_modified": False, "old_video_reused": False, "missing_source_artifacts": missing})
    return store
