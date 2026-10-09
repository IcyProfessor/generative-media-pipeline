from pathlib import Path
import shutil
import pytest
from genmedia.store import write_json,read_json,JobStore
from genmedia.pipeline.orchestrator import Pipeline,digest
from genmedia.pipeline.migration import migrate_v2
from genmedia.errors import PipelineError


def test_v2_migration_preserves_source_and_rebuilds_offline(cfg,tmp_path):
    source=tmp_path/"v2_job"
    sample=Path(__file__).resolve().parents[1]/"examples/sample_job"
    shutil.copytree(sample,source)
    write_json(source/"config.json",{"manifest":cfg.manifest,"output_dir":"unused","labels":list("ABCDEF"),
                                      "provider":"fixture","render":cfg.render.model_dump()})
    write_json(source/"job.json",{"version":1,"status":"completed","task_id":"fixture-task","candidates":[
        {"id":f"fixture-{i}","url":f"fixture://{i}","extension":"wav"} for i in (0,1)]})
    fingerprints={str(p.relative_to(source)):digest(p) for p in source.rglob("*") if p.is_file()}
    new=migrate_v2(source,tmp_path/"new_output")
    assert new.path != source and read_json(new.path/"job.json")["offline_only"]
    assert fingerprints=={str(p.relative_to(source)):digest(p) for p in source.rglob("*") if p.is_file()}
    with Pipeline(new) as p:
        with pytest.raises(PipelineError,match="replay-only"): p.start()
        for i in (0,1):
            assert p.prepare(i,offline=True,rebuild=True)
            assert read_json(new.path/f"v{i}"/"scene_plan.json")["version"]==2
            assert not (new.path/f"v{i}"/"final.mp4").exists()
    assert read_json(new.path/"migration.json")["source_modified"] is False


def test_old_job_not_silently_resumed(tmp_path):
    write_json(tmp_path/"job.json",{"version":1})
    write_json(tmp_path/"config.json",{"manifest":"not-relevant"})
    with pytest.raises(PipelineError,match="migrate"):
        JobStore(tmp_path).config
