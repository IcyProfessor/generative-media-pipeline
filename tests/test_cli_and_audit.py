import ast
import importlib.util
from pathlib import Path
import pytest
from genmedia.cli import main
from genmedia.store import write_json,read_json
from genmedia.assets.legacy import import_catalog
from genmedia.config import builtin_config,load_config
from genmedia.errors import QualityError,PipelineError


def test_cli_doctor(capsys):
    assert main(["doctor"])==0
    assert "Python:" in capsys.readouterr().out


def test_cli_live_requires_acknowledgment():
    with pytest.raises(SystemExit): main(["run","--live"])


def test_no_implicit_parent_dotenv(tmp_path,monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY",raising=False)
    (tmp_path/".env").write_text("GEMINI_API_KEY=must-not-be-loaded\n")
    child=tmp_path/"child"; child.mkdir(); monkeypatch.chdir(child)
    assert main(["doctor"])==0
    import os
    assert "GEMINI_API_KEY" not in os.environ


def test_default_data_not_dependent_on_current_directory(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg=builtin_config("counting")
    assert Path(cfg.manifest).is_file()
    assert cfg.recipe.name=="counting"


def test_legacy_metadata_import(tmp_path):
    text='A\napple.gif\nApple - "D:/old/alpha/A/apple.gif"\nobject: apple | mood: happy\n\nBackgrounds:\nsky.jpg\nsetting: sky | mood: cheerful\n"D:/old/alpha/.backgrounds/sky.jpg"\n'
    source=tmp_path/"old.txt"; source.write_text(text)
    manifest,warnings=import_catalog(source,tmp_path/"media","D:/old/alpha")
    assert len(manifest.assets)==2
    assert manifest.assets[0].path=="A/apple.gif"
    assert manifest.assets[1].path==".backgrounds/sky.jpg"
    assert manifest.assets[1].tags[0]=="setting: sky"
    assert not warnings


def audit_module():
    path=Path(__file__).resolve().parents[1]/"scripts/audit_outputs.py"
    spec=importlib.util.spec_from_file_location("audit",path)
    audit=importlib.util.module_from_spec(spec); spec.loader.exec_module(audit)
    return audit


def test_audit_nonfinite_not_silently_summed(tmp_path):
    path=tmp_path/"scene.json"; path.write_text('[{"letter":"A","duration":NaN}]')
    result=audit_module().scene_info(path,["A"])
    assert result["json_parseable"] and not result["schema_ok"] and result["duration"] is None


def test_audit_preserved_files_not_executions(tmp_path):
    job=tmp_path/"job"; job.mkdir(); (tmp_path/"unrelated").mkdir()
    (job/"final_video_v0.mp4").write_bytes(b"")
    result=audit_module().audit(tmp_path)
    assert result["observed_directories"]==2 and result["directories_with_artifacts"]==1
    assert result["artifact_counts"]["video_present"]==1 and result["artifact_counts"]["video_nonempty"]==0
    assert "NOT attempts" in result["interpretation"]


def test_legacy_plan_paths_and_explicit_tail(catalog,recipe,script,transcript):
    from genmedia.pipeline.legacy_replay import convert_plan
    duration=transcript.spans[-1].end+1
    timing=recipe.timeline(script,transcript,duration,catalog)
    visuals=recipe.fixture_visuals(catalog,script)
    old=[{"letter":t.id,"duration":t.duration,"animal":
        "D:/old/"+catalog.by_id[v.asset_id].path if v.asset_id else "",
        "background":"D:/old/"+catalog.by_id[v.background_id].path,"letter_color":v.text_color}
        for t,v in zip(timing,visuals.scenes,strict=True)]
    new,adjustment=convert_plan(old,catalog,recipe.options.labels,duration+.5)
    assert adjustment==pytest.approx(.5)
    assert new.scenes[-1].end==duration+.5 and new.version==2
    with pytest.raises(QualityError): convert_plan(old,catalog,recipe.options.labels,duration+100)


def test_python312_syntax():
    for path in (Path(__file__).resolve().parents[1]/"src").rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"),filename=str(path),feature_version=(3,12))


@pytest.mark.parametrize("tail",[float("nan"),float("inf"),-1.0])
def test_legacy_replay_rejects_invalid_tolerance(catalog,tail):
    from genmedia.pipeline.legacy_replay import convert_plan
    with pytest.raises(QualityError,match="finite"):
        convert_plan([],catalog,list("ABCDEF"),10,tail)


def test_old_config_requires_explicit_migration(tmp_path):
    path=tmp_path/"v2.yaml"
    path.write_text('manifest: x.json\nlabels: [A, B]\n')
    with pytest.raises(ValueError,match="v0.2"): load_config(path)


def test_unknown_recipe_rejected(tmp_path):
    from genmedia.models import RecipeSpec
    from genmedia.recipes import get_recipe
    with pytest.raises(PipelineError,match="Unknown recipe"):
        get_recipe(RecipeSpec(name="not_installed"))


def test_cli_mutually_exclusive_recipe_and_config():
    assert main(["demo","--recipe","counting","--config","configs/alphabet.yaml"])==1
