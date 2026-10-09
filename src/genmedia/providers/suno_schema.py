"""Pure conversion of saved Suno payloads; no SDK, HTTP client or credentials."""
import re
import math
from ..models import Script, Span, Transcript
from ..errors import QualityError

def normalize_suno(raw: list[dict]) -> Transcript:
    if not isinstance(raw, list):
        raise QualityError("invalid_provider_timestamp_list")

    spans = []

    for item in raw:
        if not isinstance(item, dict):
            raise QualityError("invalid_provider_timestamp")

        if item.get("success") is False:
            continue

        word = item.get("word", "")
        if not isinstance(word, str):
            raise QualityError("invalid_provider_timestamp_text")
        if "success" in item and not isinstance(item["success"], bool):
            raise QualityError("invalid_provider_timestamp_success")

        text = re.sub(
            r"\[[^\]]*\]",
            "",
            word,
        )
        text = " ".join(text.split())

        if not text:
            continue

        start = item.get("start_s")
        end = item.get("end_s")

        # Live SunoAPI currently also returns camelCase.
        if start is None:
            start = item.get("startS")
        if end is None:
            end = item.get("endS")

        if isinstance(start, bool) or isinstance(end, bool):
            raise QualityError("invalid_provider_timestamp")
        try:
            start = float(start)
            end = float(end)
        except (ValueError, TypeError):
            raise QualityError("invalid_provider_timestamp") from None

        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end < start
        ):
            raise QualityError("invalid_provider_timestamp")

        # Suno may return success=True with a zero-duration word.
        # Preserve the observed token while giving it the smallest useful span.
        if end == start:
            end = start + 0.001

        try:
            spans.append(Span(text=text, start=start, end=end))
        except (ValueError, TypeError):
            raise QualityError("invalid_provider_timestamp") from None

    if not spans:
        raise QualityError("empty_timestamped_lyrics")

    return Transcript(
        provider="suno",
        spans=spans,
    )

def song_text(script: Script) -> str:
    sections = {"intro": "Intro", "verse": "Verse", "chorus": "Chorus", "outro": "Outro", "speech": "Spoken Word"}
    return "\n".join(f"[{sections[s.audio_section]}]\n{s.text}" for s in script.segments)
