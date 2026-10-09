"""A test-only third policy proves the core has no INTRO/A-Z/number dependency."""
from pathlib import Path
import pytest
from genmedia.models import Script, Segment, ScenePlan, Scene, TextLayer, Box, VisualPlan, Visual, RecipeSpec
from genmedia.providers.base import Prompt
from genmedia.pipeline.timeline import partition_timeline
from genmedia.pipeline.orchestrator import create_job,Pipeline
from genmedia.recipes import register_recipe,get_recipe


class StoryFixture:
    name="story_test"
    version=7
    def __init__(self, options=None): pass
    def validate_catalog(self,catalog): catalog.validate()
    def fixture_script(self,catalog,seed):
        return Script(title="Neutral contract test",segments=[Segment(id="chapter_one",text="Hello world."),
                                                              Segment(id="chapter_two",text="Another idea.")])
    def script_prompt(self,catalog,seed): return Prompt("script","test fixture",{})
    def validate_script(self,script,catalog):
        assert [s.id for s in script.segments]==["chapter_one","chapter_two"]
    def timeline(self,script,transcript,duration,catalog):
        return partition_timeline([("chapter_one",0),("chapter_two",transcript.spans[2].start)],duration)
    def fixture_visuals(self,catalog,script):
        return VisualPlan(scenes=[Visual(id=s.id,background_id="sky") for s in script.segments])
    def visual_prompt(self,catalog,script,transcript):
        return Prompt("visuals","test fixture",{"script":script.model_dump()})
    def compile_plan(self,script,visuals,timing,catalog,duration):
        return ScenePlan(duration=duration,scenes=[Scene(**t.model_dump(),background_id=v.background_id,
            layers=[TextLayer(text=s.text,box=Box(x=.1,y=.2,width=.8,height=.5))])
            for s,v,t in zip(script.segments,visuals.scenes,timing,strict=True)])


@pytest.mark.integration
def test_third_policy_without_touching_core_or_renderer(cfg,monkeypatch):
    import genmedia.recipes as registry
    monkeypatch.setitem(registry._FACTORIES,StoryFixture.name,StoryFixture)
    monkeypatch.setenv("PYTHONPATH",str(Path(__file__).resolve().parents[1]/"src"))
    cfg.recipe=RecipeSpec(name=StoryFixture.name)
    store=create_job(cfg)
    with Pipeline(store) as p:
        assert p.run_local()["status"]=="completed"
        assert [s.id for s in p.script.segments]==["chapter_one","chapter_two"]


def test_recipe_version_mismatch_cannot_resume(cfg,monkeypatch):
    from genmedia.errors import PipelineError
    import genmedia.recipes as registry
    monkeypatch.setitem(registry._FACTORIES,StoryFixture.name,StoryFixture)
    cfg.recipe=RecipeSpec(name=StoryFixture.name)
    store=create_job(cfg)
    changed=StoryFixture(); changed.version=8
    with pytest.raises(PipelineError,match="version changed"):
        Pipeline(store,recipe=changed)


def test_registry_cannot_override_builtins():
    with pytest.raises(ValueError): register_recipe("alphabet",StoryFixture)


def test_engine_imports_no_concrete_live_adapter():
    import inspect
    from genmedia.pipeline import orchestrator
    source=inspect.getsource(orchestrator)
    assert "normalize_suno" not in source
    assert "letter_markers" not in source and "number_markers" not in source
    assert "from ..providers.suno" not in source and "from ..providers.gemini" not in source
