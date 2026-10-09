"""Regression cases found by the rc2 publication audit. No live HTTP calls."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from genmedia.errors import ProviderError, QualityError, SubmissionRejected, SubmissionUncertain
from genmedia.models import Script, TextLayer
from genmedia.pipeline.orchestrator import Pipeline, create_job
from genmedia.providers.fixture import FixtureAudio
from genmedia.providers.gemini import GeminiText, _gemini_json_schema
from genmedia.providers.suno import SunoAPI
from genmedia.providers.suno_schema import normalize_suno
from genmedia.rendering.renderer import Canvas, Sprite
from genmedia.security import redact, render_environment
from genmedia.store import read_json


@pytest.mark.parametrize("response", [
    httpx.Response(500), httpx.Response(503), httpx.Response(408),
    httpx.Response(302, headers={"location": "https://example.org"}),
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={"code": 200, "data": None}),
    httpx.Response(200, json={"code": "200", "data": {"taskId": "x"}}),
    httpx.Response(200, json={"code": 500, "data": {}}),
    httpx.Response(200, json={"code": 400, "data": {"taskId": "x"}}),
])
def test_submit_ambiguous_responses_are_never_known_refusals(cfg, response):
    calls = []
    def handler(request):
        calls.append(request)
        return response
    api = SunoAPI(cfg.suno, client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(SubmissionUncertain):
            api.request("POST", "/generate", json={})
        assert len(calls) == 1
    finally:
        api.close()


@pytest.mark.parametrize("code", [400, 401, 402, 403, 422, 429])
def test_explicit_submit_refusal(cfg, code):
    api = SunoAPI(cfg.suno, client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"code": code, "data": {}}))))
    try:
        with pytest.raises(SubmissionRejected):
            api.request("POST", "/generate", json={})
    finally:
        api.close()


@pytest.mark.parametrize("error_type", [RuntimeError, ProviderError])
def test_generic_submit_error_blocks_second_submit(cfg, error_type):
    class Ambiguous(FixtureAudio):
        calls = 0
        def submit(self, script, audio_cfg):
            self.calls += 1
            raise error_type("not evidence of a refusal")
    audio = Ambiguous()
    store = create_job(cfg)
    with Pipeline(store, audio_provider=audio) as p:
        for _ in range(2):
            with pytest.raises(SubmissionUncertain):
                p.start()
    assert audio.calls == 1
    assert read_json(store.path / "job.json")["status"] == "submission_uncertain"


def test_audio_preflight_happens_before_paid_text(cfg):
    class Refused(FixtureAudio):
        def preflight_submit(self):
            raise SubmissionRejected("missing configuration")
    class NeverText:
        def generate(self, *args):
            pytest.fail("Text provider must not run before audio preflight")
        def close(self):
            pass
    with Pipeline(create_job(cfg), audio_provider=Refused(), text_provider=NeverText()) as p:
        with pytest.raises(SubmissionRejected):
            p.start()


def test_redaction_and_render_environment():
    env = {"PATH": "/safe/bin", "SUNO_API_KEY": "private-value-one", "PRIVATE_SECRET": "private-value-two",
           "SUNO_CALLBACK_URL": "https://webhook.example/secret-location", "private_password": "password-value"}
    text = "private-value-one private-value-two https://cdn.example/song?sig=hidden Authorization: Bearer hidden-token\n"
    safe = redact(text, environ=env)
    for value in ("private-value-one", "private-value-two", "hidden-token", "sig=hidden", "\n"):
        assert value not in safe
    assert render_environment(env) == {"PATH": "/safe/bin"}


def test_schema_keyword_named_fields_are_preserved():
    class KeywordFields(BaseModel):
        default: str
        title: str
        pattern: str
    wire = _gemini_json_schema(KeywordFields)
    assert set(wire["properties"]) == {"default", "title", "pattern"}
    assert set(wire["required"]) == {"default", "title", "pattern"}


def test_no_automatic_function_calling_in_text_adapter(cfg, recipe, catalog, script):
    captured = []
    def generate_content(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(parsed=script, text=script.model_dump_json())
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content), close=lambda: None)
    with_provider = GeminiText(cfg.gemini.model, client=client)
    assert with_provider.generate(recipe.script_prompt(catalog, 42), Script) == script
    assert captured[0]["config"]["automatic_function_calling"] == {"disable": True}


@pytest.mark.parametrize("replacement", [
    {"word": None}, {"success": "true"}, {"startS": True},
    {"startS": float("nan")}, {"endS": float("inf")}, {"endS": -1},
])
def test_invalid_timestamp_fields_are_safe_quality_errors(replacement):
    row = {"word": "apple", "success": True, "startS": 1, "endS": 2}
    row.update(replacement)
    with pytest.raises(QualityError):
        normalize_suno([row])


def test_disguised_unsupported_image_decoder_is_not_used(tmp_path):
    path = tmp_path / "not-really.png"
    with Image.new("RGB", (10, 10)) as image:
        image.save(path, format="BMP")
    with pytest.raises(UnidentifiedImageError):
        Sprite(path, (20, 20))


def test_sprite_cache_is_bounded_and_upscale_works(tmp_path, monkeypatch):
    frames = [Image.new("RGBA", (8, 8), (i * 20, 50, 100, 255)) for i in range(8)]
    path = tmp_path / "generated.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    for frame in frames:
        frame.close()
    monkeypatch.setattr(Sprite, "FRAME_CACHE_BYTES", 2 * 32 * 32 * 4)
    sprite = Sprite(path, (32, 32))
    try:
        for t in [0.01, 0.11, 0.41, 0.21, 0.71, 0.01]:
            assert sprite.frame(t).size == (32, 32)
            assert sprite._cache_bytes <= sprite.FRAME_CACHE_BYTES
        assert len(sprite._cache) <= 2
    finally:
        sprite.close()
    assert sprite._source is None and sprite._cache_bytes == 0


def test_canvas_close_releases_retained_resources(cfg, catalog, plan, transcript):
    canvas = Canvas(plan, catalog, transcript.spans, cfg.render)
    canvas.frame(2)
    sprites = list(canvas.sprites.values())
    canvas.close()
    canvas.close()
    assert not canvas.sprites and not canvas.backgrounds and canvas.cache_bytes == 0
    assert all(sprite._source is None for sprite in sprites)
    assert canvas.text_image.cache_info().currsize == 0


def test_alphabet_palette_intro_and_rebuild_are_deterministic(cfg, catalog, recipe, script, transcript):
    timing = recipe.timeline(script, transcript, transcript.spans[-1].end + 1, catalog)
    visuals = recipe.fixture_visuals(catalog, script)
    a = recipe.compile_plan(script, visuals, timing, catalog, timing[-1].end)
    b = recipe.compile_plan(script, visuals, timing, catalog, timing[-1].end)
    assert a == b
    assert len([x for x in a.scenes[0].layers if x.type == "image"]) == 3
    assert [s.id for s in a.scenes] == [s.id for s in script.segments]
    colors = [x.color for s in a.scenes[1:-1] for x in s.layers if isinstance(x, TextLayer)]
    assert len(colors) == len(recipe.options.labels)
    assert all(x not in {"#000000", "#FFFFFF"} for x in colors)


def test_broken_console_does_not_block_child(tmp_path, monkeypatch):
    import genmedia.rendering.process as process
    (tmp_path / "v0").mkdir()
    original = subprocess.Popen
    def launch(command, **kwargs):
        return original([sys.executable, "-c", "print('x' * 200000)"], **kwargs)
    class BrokenConsole:
        def write(self, value):
            raise BrokenPipeError("closed by consumer")
        def flush(self):
            pass
    monkeypatch.setattr(process.subprocess, "Popen", launch)
    monkeypatch.setattr(process.sys, "stdout", BrokenConsole())
    process.run_render(tmp_path, 0, 10)
    assert (tmp_path / "v0" / "render.log").stat().st_size >= 200000
