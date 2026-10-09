import pytest
from genmedia.recipes.counting import CountingRecipe
from genmedia.recipes.alphabet import AlphabetRecipe
from genmedia.models import Transcript, ImageLayer, TextLayer, Candidate
from genmedia.providers.fixture import fixture_spans
from genmedia.errors import QualityError


@pytest.mark.parametrize("numbers", [[1], [1, 2, 3], list(range(1,11)), list(range(1,21))])
def test_counting_exact_quantity_in_layers(catalog, numbers):
    recipe = CountingRecipe({"numbers": numbers})
    recipe.validate_catalog(catalog)
    script = recipe.fixture_script(catalog,42)
    transcript = Transcript(provider="fixture", spans=fixture_spans(script, Candidate(id="fixture-0",url="fixture://0")))
    duration = transcript.spans[-1].end+1
    timing = recipe.timeline(script,transcript,duration,catalog)
    plan = recipe.compile_plan(script,recipe.fixture_visuals(catalog,script),timing,catalog,duration)
    for n, scene in zip(numbers,plan.scenes[1:-1],strict=True):
        images = [layer for layer in scene.layers if isinstance(layer,ImageLayer)]
        assert len(images) == n
        assert len({(x.box.x,x.box.y) for x in images}) == n
        assert next(x.text for x in scene.layers if isinstance(x,TextLayer)) == str(n)
        assert all(x.box.x+x.box.width <= 1 and x.box.y+x.box.height <= .8 for x in images)


def test_counting_does_not_require_letter_metadata(catalog):
    for a in catalog.by_id.values():
        a.label = None
    r = CountingRecipe({"numbers": [1,2]})
    r.validate_catalog(catalog)
    r.validate_script(r.fixture_script(catalog,42),catalog)


def test_counting_requires_explicit_plural(catalog):
    for a in catalog.by_id.values():
        a.plural = None
    with pytest.raises(QualityError,match="plural"):
        CountingRecipe({"numbers":[1,2]}).validate_catalog(catalog)


def test_counting_rejects_wrong_quantity(catalog):
    r = CountingRecipe({"numbers":[1,2,3]})
    s = r.fixture_script(catalog,42)
    s.segments[2].text = s.segments[2].text.replace("Two", "Three")
    with pytest.raises(QualityError):
        r.validate_script(s,catalog)


def test_counting_rejects_wrong_plural(catalog):
    r = CountingRecipe({"numbers":[1,2]})
    s = r.fixture_script(catalog,42)
    s.segments[2].text = "Two unicorns, dancing happily!"
    with pytest.raises(QualityError,match="mismatch"):
        r.validate_script(s,catalog)


def test_counting_missing_number_in_audio_rejected(catalog):
    r = CountingRecipe({"numbers":[1,2,3]})
    s = r.fixture_script(catalog,42)
    spans = fixture_spans(s,Candidate(id="fixture-0",url="fixture://0"))
    t = Transcript(provider="fixture",spans=[x for x in spans if x.text != "Two"])
    with pytest.raises(QualityError,match="coverage"):
        r.timeline(s,t,t.spans[-1].end+1,catalog)


def test_counting_digit_transcript_allowed(catalog):
    r = CountingRecipe({"numbers":[1,2,3]})
    s = r.fixture_script(catalog,42)
    spans = fixture_spans(s,Candidate(id="fixture-0",url="fixture://0"))
    for x in spans:
        x.text = {"One":"1","Two":"2","Three":"3"}.get(x.text,x.text)
    t = Transcript(provider="test",spans=spans)
    assert len(r.timeline(s,t,t.spans[-1].end+1,catalog)) == 5


def test_counting_extra_number_rejected(catalog):
    r = CountingRecipe({"numbers":[1,2]})
    s = r.fixture_script(catalog,42)
    s.segments[1].text += " One more!"
    with pytest.raises(QualityError):
        r.validate_script(s,catalog)


def test_alphabet_full_fails_before_api_with_demo_catalog(catalog):
    with pytest.raises(QualityError,match="label G"):
        AlphabetRecipe({"labels": list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}).validate_catalog(catalog)


def test_recipes_are_deterministic_for_fixture_seed(catalog):
    for r in [AlphabetRecipe(),CountingRecipe()]:
        assert r.fixture_script(catalog,42) == r.fixture_script(catalog,42)
        assert all("path" not in a for a in r.script_prompt(catalog,42).data["catalog"])


@pytest.mark.parametrize("recipe", [AlphabetRecipe(), CountingRecipe({"numbers":[1,2]})])
def test_song_section_hints_cannot_silently_turn_into_speech(catalog, recipe):
    script = recipe.fixture_script(catalog, 42)
    script.segments[1].audio_section = "speech"
    with pytest.raises(QualityError, match="sections"):
        recipe.validate_script(script, catalog)


def test_full_alphabet_and_historical_missing_last_five_regression(tmp_path, catalog):
    from pathlib import Path
    from genmedia.assets.manifest import Catalog
    from genmedia.models import Asset, Manifest
    from genmedia.store import write_json
    # Synthetic metadata-only catalog. Reused geometric PNGs are NOT evidence of
    # pedagogically correct visuals; this regression tests A-Z timing/coverage.
    objects = "apple bear cat dog elephant fish goat horse igloo jellyfish kite lion mouse nest owl pig queen rabbit sun turtle umbrella violin whale xylophone yak zebra".split()
    assets = [Asset(id="test_"+label, kind="character", label=label, object=name,
                    path="characters/apple.png") for label, name in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", objects, strict=True)]
    assets += [Asset(id="sky", kind="background", path="backgrounds/sky.png")]
    manifest = tmp_path/"full-alphabet.json"
    write_json(manifest, Manifest(root=str(catalog.root), assets=assets).model_dump())
    full = Catalog(manifest)
    recipe = AlphabetRecipe({"labels": list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")})
    recipe.validate_catalog(full)
    script = recipe.fixture_script(full, 42)
    transcript = Transcript(provider="fixture", spans=fixture_spans(script, Candidate(id="fixture-0", url="fixture://0")))
    duration = transcript.spans[-1].end+1
    assert len(recipe.timeline(script, transcript, duration, full)) == 28
    broken = script.model_copy(deep=True)
    broken.segments = script.segments[:22]+script.segments[-1:]
    truncated = Transcript(provider="fixture", spans=fixture_spans(broken, Candidate(id="fixture-0", url="fixture://0")))
    with pytest.raises(QualityError, match="missing=.*V.*W.*X.*Y.*Z"):
        recipe.timeline(script, truncated, truncated.spans[-1].end+1, full)
