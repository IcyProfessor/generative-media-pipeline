"""Small ports. Live adapters do not import or branch on recipes."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar
from pydantic import BaseModel
from ..models import AudioConfig, Script, Candidate, Transcript

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Prompt:
    purpose: str
    instructions: str
    data: dict[str, Any]
    temperature: float = 0.3


@dataclass(frozen=True)
class AlignmentRequest:
    task_id: str
    candidate: Candidate
    audio_path: Path
    script: Script


class TextProvider(Protocol):
    def generate(self, prompt: Prompt, schema: type[T]) -> T: ...
    def close(self) -> None: ...


class AudioProvider(Protocol):
    """Submit returns a task ID. Only SubmissionRejected proves a safe refusal.

    All other failures during submit are conservatively treated as uncertain.
    preflight_submit must not create a task or perform a billable action.
    """
    def preflight_submit(self) -> None: ...
    def submit(self, script: Script, cfg: AudioConfig) -> str: ...
    def poll(self, task_id: str) -> list[Candidate] | None: ...
    def download(self, candidate: Candidate, path: Path) -> None: ...
    def close(self) -> None: ...


class TimestampProvider(Protocol):
    billable: bool
    def align(self, request: AlignmentRequest) -> Transcript: ...
    def close(self) -> None: ...


class RenderRunner(Protocol):
    def __call__(self, job: Path, branch: int, timeout: int) -> None: ...
