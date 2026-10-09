from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import pytest
from genmedia.store import read_json, write_json
from genmedia.pipeline.orchestrator import create_job, Pipeline, digest
from genmedia.providers.fixture import FixtureAudio, FixtureAlignment
from genmedia.models import RecipeSpec, Config, Transcript
from genmedia.errors import SubmissionUncertain, PipelineError, ProviderError, SubmissionRejected
from genmedia.media import verify_video


def prepared(cfg):
    store = create_job(cfg)
    with Pipeline(store) as p:
        p.start(); p.poll()
        assert p.prepare(0)
    return store


def test_checkpoints_and_idempotent_start(cfg, script):
    class Counting(FixtureAudio):
        submitted = 0
        def submit(self, script, cfg):
            self.submitted += 1
            return super().submit(script,cfg)
    audio = Counting(script)
    store = create_job(cfg)
    with Pipeline(store,audio_provider=audio) as p:
        p.start(); p.start()
        assert audio.submitted == 1
        assert p.poll() == [0,1]
        assert p.prepare(0) and p.prepare(1)
        assert p.prepare(0,offline=True)
    assert (store.path/"script.txt").exists()
    assert (store.path/"v0/transcript.json").exists()
    assert read_json(store.path/"v0/validation.json")["acoustic_correctness_checked"] is False


def test_unknown_submission_not_retried(cfg,script):
    class Uncertain(FixtureAudio):
        submitted = 0
        def submit(self,script,cfg):
            self.submitted += 1
            raise SubmissionUncertain("simulated post-acceptance network failure")
    audio = Uncertain(script)
    store = create_job(cfg)
    with Pipeline(store,audio_provider=audio) as p:
        with pytest.raises(SubmissionUncertain): p.start()
        with pytest.raises(SubmissionUncertain): p.start()
        assert audio.submitted == 1
        p.attach_task("reconciled-task")
        assert p.start() == "reconciled-task"
        assert audio.submitted == 1

def test_known_submission_rejection_can_be_retried(cfg, script):
    class Rejected(FixtureAudio):
        submitted = 0

        def submit(self, script, cfg):
            self.submitted += 1
            raise SubmissionRejected("simulated known provider rejection")

    audio = Rejected(script)
    store = create_job(cfg)

    with Pipeline(store, audio_provider=audio) as p:
        with pytest.raises(ProviderError):
            p.start()

        state = read_json(store.path / "job.json")
        assert state["status"] == "submission_rejected"
        assert audio.submitted == 1

        with pytest.raises(ProviderError):
            p.start()

        assert audio.submitted == 2

def test_interrupt_during_paid_submit_also_blocks_resume(cfg,script):
    class Interrupted(FixtureAudio):
        def submit(self,script,cfg): raise KeyboardInterrupt
    store = create_job(cfg)
    with Pipeline(store,audio_provider=Interrupted(script)) as p:
        with pytest.raises(KeyboardInterrupt): p.start()
        assert read_json(store.path/"job.json")["status"] == "submitting"
        with pytest.raises(SubmissionUncertain): p.start()


def test_crash_after_candidate_commit_recovers(cfg):
    store = create_job(cfg)
    with Pipeline(store) as p:
        p.start(); p.poll()
        (store.path/"v1/branch.json").unlink()
        assert p.poll() == [0,1]
        assert read_json(store.path/"v1/branch.json")["status"] == "pending"


def test_one_bad_variant_does_not_stop_other(cfg):
    class Truncated(FixtureAlignment):
        def align(self, request):
            t = super().align(request)
            if request.candidate.id.endswith("0"):
                t.spans = [x for x in t.spans if x.text != "F"]
            return t
    store = create_job(cfg)
    with Pipeline(store,timestamp_provider=Truncated()) as p:
        p.start(); p.poll()
        assert not p.prepare(0)
        assert p.prepare(1)
        assert not p.render(0)
    assert read_json(store.path/"v0/branch.json")["status"] == "rejected"


class NoCalls:
    def __getattr__(self,key):
        raise AssertionError(f"Offline attempted provider access: {key}")
    def close(self): pass


def test_offline_does_not_call_any_provider(cfg):
    store = prepared(cfg)
    with Pipeline(store,text_provider=NoCalls(),audio_provider=NoCalls(),timestamp_provider=NoCalls()) as p:
        assert p.prepare(0,offline=True,rebuild=True)


@pytest.mark.parametrize("missing", ["audio.wav","transcript.json","visuals.json"])
def test_offline_missing_artifact_fails_not_api(cfg,missing):
    store = prepared(cfg)
    (store.path/"v0"/missing).unlink()
    with Pipeline(store,text_provider=NoCalls(),audio_provider=NoCalls(),timestamp_provider=NoCalls()) as p:
        assert not p.prepare(0,offline=True,rebuild=True)
    assert "Offline replay needs" in read_json(store.path/"v0/branch.json")["error"]


def test_poll_deadline(cfg):
    store = create_job(cfg)
    with Pipeline(store) as p:
        p.start()
        with store.lock(): store.update(poll_deadline=0)
        with pytest.raises(PipelineError,match="deadline"): p.poll()


def test_atomic_writer_preserves_old_on_bad_json(tmp_path):
    path = tmp_path/"state.json"
    write_json(path,{"counter":1})
    with pytest.raises(ValueError): write_json(path,{"bad":float("nan")})
    assert read_json(path) == {"counter":1}
    assert not list(tmp_path.glob(".write-*"))


def test_branch_lock_no_lost_updates(cfg):
    store = create_job(cfg)
    def update(_):
        with store.lock(0):
            path = store.branch(0)/"branch.json"
            count = read_json(path).get("counter",0) if path.exists() else 0
            store.update(branch=0,counter=count+1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update,range(20)))
    assert read_json(store.path/"v0/branch.json")["counter"] == 20


@pytest.mark.integration
@pytest.mark.parametrize("name", ["alphabet","counting"])
def test_real_two_variant_render_replay_and_cache(cfg,monkeypatch,name):
    cfg.recipe = RecipeSpec(name=name)
    monkeypatch.setenv("PYTHONPATH",str(Path(__file__).resolve().parents[1]/"src"))
    store = create_job(cfg)
    with Pipeline(store) as p:
        assert p.run_local()["status"] == "completed"
        for index in (0,1):
            file = store.path/f"v{index}"/"final.mp4"
            verify_video(file)
            before = file.stat().st_mtime_ns
            assert p.render(index)
            assert p.prepare(index,offline=True,rebuild=True)
            assert p.render(index)
            assert file.stat().st_mtime_ns == before
            assert read_json(store.path/f"v{index}"/"render_result.json")["decode_verified"]


def test_independent_timestamp_provider(cfg):
    class TimestampOnly(FixtureAlignment):
        called = 0
        def align(self,request):
            assert request.audio_path.is_file()
            self.called += 1
            return super().align(request)
    timestamps = TimestampOnly()
    store = create_job(cfg)
    with Pipeline(store,timestamp_provider=timestamps) as p:
        p.start(); p.poll()
        assert p.prepare(0)
    assert timestamps.called == 1


def test_invalid_cached_plan_isolates_branch(cfg):
    store = prepared(cfg)
    (store.path/"v0/scene_plan.json").write_text("{}",encoding="utf-8")
    with Pipeline(store) as p:
        assert not p.prepare(0,offline=True)
        assert p.prepare(1)
    assert read_json(store.path/"v0/branch.json")["status"] == "rejected"


def test_changed_config_refuses_resume(cfg):
    store = create_job(cfg)
    data = read_json(store.path/"config.json")
    data["recipe"] = {"name":"counting","options":{}}
    write_json(store.path/"config.json",data)
    with pytest.raises(PipelineError): Pipeline(store)


def test_script_mutation_after_submit_rejected(cfg):
    store = prepared(cfg)
    data = read_json(store.path/"script.json")
    data["title"] = "manually changed"
    write_json(store.path/"script.json",data)
    with Pipeline(store) as p:
        assert not p.prepare(0,offline=True,rebuild=True)
    assert "script changed" in read_json(store.path/"v0/branch.json")["error"]


def test_alignment_uncertain_requires_explicit_consent(cfg):
    class Paid(FixtureAlignment):
        billable = True
        calls = 0
        def align(self,request):
            self.calls += 1
            if self.calls == 1: raise RuntimeError("possibly charged")
            return super().align(request)
    aligner = Paid()
    store = create_job(cfg)
    with Pipeline(store,timestamp_provider=aligner) as p:
        p.start(); p.poll()
        assert not p.prepare(0)
        assert not p.prepare(0)
        assert aligner.calls == 1
        assert p.prepare(0,retry_alignment=True)
        assert aligner.calls == 2


def test_renderer_failure_is_resumable_without_regeneration(cfg):
    store = prepared(cfg)
    def broken(*args): raise RuntimeError("encode failed")
    with Pipeline(store,render_runner=broken) as p:
        assert not p.render(0)
    assert read_json(store.path/"v0/branch.json")["status"] == "render_failed"
    with Pipeline(store,text_provider=NoCalls(),audio_provider=NoCalls(),timestamp_provider=NoCalls()) as p:
        assert p.prepare(0,offline=True)


def test_asset_change_blocks_render_even_with_cached_output(cfg,tmp_path):
    import shutil
    source = Path(cfg.manifest).parent
    target = tmp_path/"assets"
    shutil.copytree(source,target)
    cfg.manifest = str(target/"manifest.json")
    store = prepared(cfg)
    (target/"characters/apple.png").write_bytes(b"changed")
    with Pipeline(store) as p:
        assert not p.render(0)
    assert "Asset changed" in read_json(store.path/"v0/branch.json")["error"]


def test_duplicate_candidates_rejected(cfg,script):
    class Duplicate(FixtureAudio):
        def poll(self,task):
            a = super().poll(task)[0]
            return [a,a]
    store = create_job(cfg)
    with Pipeline(store,audio_provider=Duplicate(script)) as p:
        p.start()
        with pytest.raises(PipelineError,match="duplicate"): p.poll()


def test_close_each_injected_resource_once(cfg):
    class Resource:
        calls = 0
        def close(self): self.calls += 1
    r = Resource()
    p = Pipeline(create_job(cfg),text_provider=r,audio_provider=r,timestamp_provider=r)
    p.close()
    assert r.calls == 1


@pytest.mark.integration
def test_corrupt_output_is_rerendered_but_modified_input_is_not_trusted(cfg, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]/"src"))
    store = prepared(cfg)
    with Pipeline(store) as p:
        assert p.render(0)
        output = store.path/"v0/final.mp4"
        output.write_bytes(b"interrupted transfer")
        assert p.render(0)
        verify_video(output)
        good = digest(output)
        path = store.path/"v0/visuals.json"
        data = read_json(path)
        data["scenes"][1]["text_color"] = "#000001"
        write_json(path, data)
        assert not p.render(0)
        assert digest(output) == good
        assert "Prepared artifacts changed" in read_json(store.path/"v0/branch.json")["error"]
        assert p.prepare(0, offline=True, rebuild=True)
        assert p.render(0)


def test_charged_alignment_success_with_lost_result_not_resubmitted(cfg):
    class Paid(FixtureAlignment):
        billable = True
        calls = 0
        def align(self, request):
            self.calls += 1
            return super().align(request)
    aligner = Paid()
    store = create_job(cfg)
    with Pipeline(store, timestamp_provider=aligner) as p:
        p.start(); p.poll()
        assert p.prepare(0)
        (store.path/"v0/transcript.json").unlink()
        assert not p.prepare(0, rebuild=True)
        assert aligner.calls == 1
        assert read_json(store.path/"v0/branch.json")["status"] == "alignment_uncertain"


def test_unknown_task_id_return_is_not_silently_paid_again(cfg, script):
    class Bad(FixtureAudio):
        calls = 0
        def submit(self, script, cfg):
            self.calls += 1
            return ""
    provider = Bad(script)
    store = create_job(cfg)
    with Pipeline(store, audio_provider=provider) as p:
        with pytest.raises(PipelineError): p.start()
        with pytest.raises(SubmissionUncertain): p.start()
        assert provider.calls == 1
