from pathlib import Path
import pytest
from genmedia.config import builtin_config
from genmedia.assets.manifest import Catalog
from genmedia.recipes import get_recipe
from genmedia.providers.fixture import FixtureText, FixtureAudio, fixture_spans

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path):
    cfg = builtin_config(output=tmp_path/"output")
    cfg.render.width = 320
    cfg.render.height = 180
    cfg.render.fps = 6
    return cfg


@pytest.fixture
def catalog(cfg):
    return Catalog(Path(cfg.manifest))


@pytest.fixture
def recipe(cfg):
    return get_recipe(cfg.recipe)


@pytest.fixture
def script(cfg, catalog, recipe):
    return recipe.fixture_script(catalog, cfg.seed)


@pytest.fixture
def transcript(script):
    from genmedia.models import Transcript
    candidate = FixtureAudio(script).poll("fixture-task")[0]
    return Transcript(provider="fixture", spans=fixture_spans(script, candidate))


@pytest.fixture
def raw(transcript):
    return [{"word": x.text, "startS": x.start, "endS": x.end, "success": True} for x in transcript.spans]


@pytest.fixture
def plan(cfg, catalog, recipe, script, transcript):
    duration = transcript.spans[-1].end+1
    timing = recipe.timeline(script, transcript, duration, catalog)
    return recipe.compile_plan(script, recipe.fixture_visuals(catalog, script), timing, catalog, duration)
