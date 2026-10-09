"""Offline doubles: synthetic tones, never claimed to be music or speech."""
from pathlib import Path
import math
import struct
import wave
from ..models import Script, VisualPlan, Candidate, Span, Transcript
from ..errors import ProviderError


class FixtureText:
    def __init__(self, recipe, catalog, seed=42):
        self.recipe, self.catalog, self.seed = recipe, catalog, seed

    def generate(self, prompt, schema):
        if prompt.purpose == "script" and schema is Script:
            return self.recipe.fixture_script(self.catalog, self.seed)
        if prompt.purpose == "visuals" and schema is VisualPlan:
            script = Script.model_validate(prompt.data["script"])
            return self.recipe.fixture_visuals(self.catalog, script)
        raise ProviderError("Fixture provider received an unsupported request")

    def close(self):
        pass


def fixture_spans(script: Script, candidate: Candidate) -> list[Span]:
    if candidate.id not in {"fixture-0", "fixture-1"}:
        raise ProviderError("Synthetic alignment is restricted to fixture audio candidates")
    speed = .16 if candidate.id.endswith("0") else .20
    t, result = .3, []
    for segment in script.segments:
        for word in segment.text.split():
            result.append(Span(text=word, start=round(t, 4), end=round(t+speed*.85, 4)))
            t += speed
        t += .25
    return result


class FixtureAudio:
    def __init__(self, script: Script | None = None):
        self.script = script

    def preflight_submit(self):
        pass

    def submit(self, script, cfg):
        self.script = script
        return "fixture-task"

    def poll(self, task_id):
        return [Candidate(id=f"fixture-{i}", url=f"fixture://{i}", extension="wav") for i in range(2)]

    def download(self, candidate, path: Path):
        if self.script is None:
            raise ProviderError("FixtureAudio requires a saved script")
        length = fixture_spans(self.script, candidate)[-1].end + .8
        rate = 16000
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            # Small bounded blocks, including when a full 1..20/A..Z fixture is used.
            for offset in range(0, math.ceil(length * rate), rate):
                data = bytearray()
                for i in range(offset, min(offset+rate, math.ceil(length*rate))):
                    t = i / rate
                    freq = [261.63, 293.66, 329.63, 349.23, 392, 440][int(t) % 6]
                    envelope = min(1, (t % 1) * 12) * max(0, 1-t % 1)
                    value = int(2500 * math.sin(2 * math.pi * freq * t) * envelope)
                    data.extend(struct.pack("<h", value))
                out.writeframes(data)

    def close(self):
        pass


class FixtureAlignment:
    billable = False

    def align(self, request):
        return Transcript(provider="fixture", spans=fixture_spans(request.script, request.candidate))

    def close(self):
        pass
