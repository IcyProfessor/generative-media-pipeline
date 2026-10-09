"""Provider-neutral timing, cue serialization and temporal validation."""
from __future__ import annotations
import math
import re
from ..errors import QualityError
from ..models import Span, Timing, Transcript


def validate_transcript(transcript: Transcript, duration: float) -> None:
    if not math.isfinite(duration) or not 0 < duration < 3600:
        raise QualityError("invalid_audio_duration")
    spans = transcript.spans
    for a, b in zip(spans, spans[1:]):
        if b.start < a.start or b.end < a.end:
            raise QualityError("non_monotonic_timestamps")
        if b.start < a.end - .02:
            raise QualityError("overlapping_timestamps")
    if spans[-1].end > duration + .1:
        raise QualityError("transcript_exceeds_audio_duration")


def partition_timeline(boundaries: list[tuple[str, float]], duration: float) -> list[Timing]:
    """Make a complete partition from recipe-defined segment starts."""
    if not boundaries or boundaries[0][1] != 0 or not math.isfinite(duration) or duration <= 0:
        raise QualityError("invalid_timeline_boundaries")
    ids = [key for key, _ in boundaries]
    starts = [t for _, t in boundaries]
    if len(ids) != len(set(ids)):
        raise QualityError("duplicate_timeline_ids")
    if any(not math.isfinite(t) or t < 0 for t in starts):
        raise QualityError("invalid_timeline_boundaries")
    ends = starts[1:] + [duration]
    if any(end <= start for start, end in zip(starts, ends, strict=True)):
        raise QualityError("ambiguous_scene_boundaries")
    return [Timing(id=key, start=start, end=end)
            for (key, start), end in zip(boundaries, ends, strict=True)]


def subtitles_from_spans(spans: list[Span], max_chars: int = 68, max_seconds: float = 5) -> list[Span]:
    """Use only observed words, never source text to fill transcript gaps."""
    if max_chars < 1 or max_seconds <= 0:
        raise ValueError("Subtitle limits must be positive")
    groups: list[Span] = []
    current: list[Span] = []
    def flush():
        if current:
            groups.append(Span(text=" ".join(x.text for x in current), start=current[0].start, end=current[-1].end))
            current.clear()
    for item in spans:
        length = sum(len(x.text) + 1 for x in current) + len(item.text)
        if current and (length > max_chars or item.end - current[0].start > max_seconds
                        or item.start - current[-1].end > 1):
            flush()
        current.append(item)
        if item.text.endswith(("!", "?", ".")):
            flush()
    flush()
    return groups


def timestamp(value: float) -> str:
    if not math.isfinite(value) or value < 0:
        raise QualityError("invalid_srt_timestamp")
    ms = round(value * 1000)
    seconds, ms = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def format_srt(cues: list[Span]) -> str:
    return "\n\n".join(f"{i}\n{timestamp(x.start)} --> {timestamp(x.end)}\n{x.text}"
                        for i, x in enumerate(cues, 1)) + "\n"


def parse_srt(text: str) -> list[Span]:
    def seconds(s: str) -> float:
        h, m, rest = s.replace(".", ",").split(":")
        sec, ms = rest.split(",")
        if not 0 <= int(m) < 60 or not 0 <= int(sec) < 60:
            raise QualityError("invalid_srt_timestamp")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000
    result = []
    for block in re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if not lines or not any(x.strip() for x in lines):
            continue
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            raise QualityError("invalid_srt_block")
        match = re.fullmatch(r"(\d{2,}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2,}:\d{2}:\d{2}[,.]\d{3})", lines[0].strip())
        if not match or len(lines) < 2:
            raise QualityError("invalid_srt_block")
        try:
            result.append(Span(text=" ".join(lines[1:]).strip(), start=seconds(match[1]), end=seconds(match[2])))
        except ValueError as exc:
            raise QualityError("invalid_srt_span") from exc
    if not result:
        raise QualityError("empty_srt")
    return result
