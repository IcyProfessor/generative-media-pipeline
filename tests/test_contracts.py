import math
from pathlib import Path
import pytest
from pydantic import ValidationError
from genmedia.models import (Span, Timing, Manifest, Asset, ScenePlan, Scene, Box,
                             TextLayer, ImageLayer, Script, Segment, Transcript)
from genmedia.errors import QualityError, PipelineError
from genmedia.assets.manifest import Catalog
from genmedia.store import write_json
from genmedia.recipes.alphabet import AlphabetOptions
from genmedia.recipes.counting import CountingOptions
from genmedia.providers.suno_schema import normalize_suno
from genmedia.pipeline.timeline import (timestamp, parse_srt, format_srt, subtitles_from_spans,
                                       validate_transcript, partition_timeline)
from genmedia.pipeline.validation import validate_plan


@pytest.mark.parametrize("value", [0, -1, math.nan, math.inf, -math.inf])
def test_scene_end_rejects_invalid(value):
    with pytest.raises(ValidationError):
        Timing(id="arbitrary_scene", start=0, end=value)


@pytest.mark.parametrize("labels", [[], ["a"], ["A", "A"], ["B", "A"], ["AA"], ["1"]])
def test_alphabet_options_reject_labels(labels):
    with pytest.raises(ValidationError):
        AlphabetOptions(labels=labels)


@pytest.mark.parametrize("numbers", [[], [0], [-1], [21], [1, 1], [2, 1], [1, 3], [1.5], [True]])
def test_counting_options_reject_numbers(numbers):
    with pytest.raises(ValidationError):
        CountingOptions(numbers=numbers)


def test_manifest_duplicate_ids():
    a = Asset(id="apple", kind="character", path="a.png", label="A")
    with pytest.raises(ValidationError):
        Manifest(assets=[a, a])


@pytest.mark.parametrize("path", ["../secret.png", "/tmp/pic.png", "C:/secret/pic.png", "C:pic.png", "folder/../../pic.png", "\\\\server\\share\\x.png"])
def test_resolver_rejects_traversal(tmp_path, path):
    write_json(tmp_path/"manifest.json", {"assets": [{"id": "x", "kind": "character", "path": path}]})
    with pytest.raises(QualityError):
        Catalog(tmp_path/"manifest.json").resolve("x")


def test_resolver_rejects_symlink_escape(tmp_path):
    (tmp_path/"catalog").mkdir()
    outside = tmp_path/"outside.png"
    outside.write_bytes(b"not a real picture")
    try:
        (tmp_path/"catalog"/"alias.png").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires permissions on this platform")
    write_json(tmp_path/"catalog"/"manifest.json", {"assets": [{"id": "x", "kind": "character", "path": "alias.png"}]})
    with pytest.raises(QualityError):
        Catalog(tmp_path/"catalog"/"manifest.json").resolve("x")


def test_catalog_valid(catalog, recipe):
    recipe.validate_catalog(catalog)
    assert len(catalog.by_id) == 9
    assert all("path" not in item for item in catalog.context())


def test_unknown_asset(catalog):
    with pytest.raises(QualityError):
        catalog.resolve("missing")


def test_script_valid(recipe, catalog, script):
    recipe.validate_script(script, catalog)


def test_script_object_mismatch(recipe, catalog, script):
    script.segments[1].text = "A is for aardvark, come and play!"
    with pytest.raises(QualityError, match="object"):
        recipe.validate_script(script, catalog)


def test_script_wrong_asset(recipe, catalog, script):
    script.segments[1].asset_id = "balloon"
    with pytest.raises(QualityError):
        recipe.validate_script(script, catalog)


def test_timeline_covers_audio(recipe, catalog, script, transcript):
    duration = transcript.spans[-1].end+2
    slots = recipe.timeline(script, transcript, duration, catalog)
    assert [x.id for x in slots] == [s.id for s in script.segments]
    assert slots[-1].end == duration
    assert sum(x.duration for x in slots) == pytest.approx(duration)
    assert all(x.end == y.start for x, y in zip(slots, slots[1:]))


def test_outro_not_last_word(recipe, catalog, script, transcript):
    slots = recipe.timeline(script, transcript, transcript.spans[-1].end+1, catalog)
    assert slots[-1].start < transcript.spans[-1].start
    assert slots[-1].start == next(x.start for x in transcript.spans if x.text == "We")


def test_missing_letter_rejected(recipe, catalog, script, transcript):
    transcript.spans = [x for x in transcript.spans if x.text != "F"]
    with pytest.raises(QualityError, match="coverage"):
        recipe.timeline(script, transcript, transcript.spans[-1].end+1, catalog)


def test_no_invented_words(raw):
    raw[5]["success"] = False
    assert len(normalize_suno(raw).spans) == len(raw)-1


def test_tags_removed():
    t = normalize_suno([{"word": "[Verse]\n A", "startS": 1, "endS": 2, "success": True}])
    assert t.spans[0].text == "A"


@pytest.mark.parametrize("raw", [[], None, [None], [{"word": "Hi"}], [{"word": "Hi", "startS": 2, "endS": 1}]])
def test_bad_raw_rejected(raw):
    with pytest.raises(QualityError):
        normalize_suno(raw)


def test_two_letters_same_cue_rejected(recipe, catalog):
    from genmedia.recipes.alphabet import AlphabetRecipe
    r = AlphabetRecipe({"labels": ["A", "B"]})
    script = r.fixture_script(catalog, 42)
    script.segments[-1].text = "We did it"
    transcript = Transcript(provider="test", granularity="segment", spans=[
        Span(text="A is for apple. B is for balloon.", start=1, end=3), Span(text="We did it", start=4, end=5)])
    with pytest.raises(QualityError, match="ambiguous"):
        r.timeline(script, transcript, 6, catalog)


def test_missing_outro_rejected(recipe, catalog, script, transcript):
    script.segments[-1].text = "Never actually sung"
    with pytest.raises(QualityError, match="outro_anchor"):
        recipe.timeline(script, transcript, transcript.spans[-1].end+1, catalog)


def test_audio_bounds(transcript):
    with pytest.raises(QualityError, match="exceeds_audio"):
        validate_transcript(transcript, 3)


def test_unordered_timestamps(transcript):
    transcript.spans[0], transcript.spans[1] = transcript.spans[1], transcript.spans[0]
    with pytest.raises(QualityError, match="non_monotonic"):
        validate_transcript(transcript, 30)


def test_overlapping_timestamps():
    t = Transcript(provider="test", spans=[Span(text="a", start=0, end=2), Span(text="b", start=1, end=3)])
    with pytest.raises(QualityError, match="overlapping"):
        validate_transcript(t, 4)


def test_spoken_object_check(recipe, catalog, script, transcript):
    next(x for x in transcript.spans if x.text.startswith("apple")).text = "aardvark,"
    with pytest.raises(QualityError, match="object differs"):
        recipe.timeline(script, transcript, transcript.spans[-1].end+1, catalog)


def test_semantic_plan_cannot_change_asset(recipe, catalog, script, transcript):
    duration = transcript.spans[-1].end+1
    timing = recipe.timeline(script, transcript, duration, catalog)
    visual = recipe.fixture_visuals(catalog, script)
    visual.scenes[1].asset_id = "fish"
    with pytest.raises(QualityError, match="changed"):
        recipe.compile_plan(script, visual, timing, catalog, duration)


def test_plan_gaps_rejected(plan, catalog):
    plan.scenes[1].start += .1
    with pytest.raises(QualityError, match="gaps"):
        validate_plan(plan, catalog)


def test_generic_plan_does_not_require_alphabet(catalog):
    plan = ScenePlan(duration=2, scenes=[Scene(id="chapter_123", start=0, end=2, background_id="sky",
        layers=[TextLayer(text="Any story", box=Box(x=.1, y=.1, width=.8, height=.5))])])
    validate_plan(plan, catalog)
    assert ScenePlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize("box", [dict(x=-.1,y=0,width=.5,height=.5), dict(x=.8,y=0,width=.5,height=.5),
                                 dict(x=0,y=0,width=0,height=.5), dict(x=0,y=math.nan,width=.5,height=.5)])
def test_layer_box_bounds(box):
    with pytest.raises(ValidationError):
        Box(**box)


def test_plan_schema_version_guard():
    with pytest.raises(ValidationError):
        ScenePlan.model_validate({"version": 1, "duration": 2, "scenes": []})


def test_unknown_layer_type_rejected():
    with pytest.raises(ValidationError):
        Scene(id="x", start=0, end=1, background_id="sky", layers=[{"type": "python", "code": "exec"}])


def test_duplicate_script_segments_rejected():
    with pytest.raises(ValidationError):
        Script(title="x", segments=[Segment(id="x",text="hi"), Segment(id="x",text="again")])


def test_partition_duplicate_ids():
    with pytest.raises(QualityError, match="duplicate"):
        partition_timeline([("x",0), ("x",1)],2)


def test_timestamp_carry():
    assert timestamp(59.9998) == "00:01:00,000"
    assert timestamp(3600) == "01:00:00,000"


def test_srt_roundtrip(transcript):
    cues = subtitles_from_spans(transcript.spans)
    parsed = parse_srt(format_srt(cues))
    assert [x.text for x in cues] == [x.text for x in parsed]
    assert parsed[-1].end == pytest.approx(cues[-1].end, abs=.001)


@pytest.mark.parametrize("text", ["", "1", "1\nwrong\nHello", "1\n00:60:00,000 --> 00:61:00,000\nHello"])
def test_bad_srt(text):
    with pytest.raises(QualityError):
        parse_srt(text)
