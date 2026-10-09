from pathlib import Path
import math
import wave
import subprocess
import mutagen
from imageio_ffmpeg import get_ffmpeg_exe
from .errors import QualityError, PipelineError


def audio_duration(path: Path) -> float:
    try:
        if path.suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
        else:
            audio = mutagen.File(path)
            duration = float(audio.info.length) if audio is not None else 0
    except Exception:
        raise QualityError("audio_metadata_unreadable") from None
    if not math.isfinite(duration) or duration <= 0:
        raise QualityError("audio_duration_invalid")
    return duration


def verify_video(path: Path, timeout: int = 300) -> None:
    """Decode ALL audio/video; a file merely existing is not render completion."""
    if not path.is_file() or path.stat().st_size == 0:
        raise PipelineError("Rendered video is missing or empty")
    result = subprocess.run([get_ffmpeg_exe(), "-v", "error", "-xerror", "-nostdin",
                             "-i", str(path), "-map", "0:v:0", "-map", "0:a:0",
                             "-f", "null", "-"], capture_output=True, timeout=timeout)
    if result.returncode:
        raise PipelineError("Rendered MP4 failed audio/video decode verification")
