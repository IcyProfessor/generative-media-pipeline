"""Atomic single-host filesystem checkpoints (not a distributed transaction)."""
from __future__ import annotations
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock
from .models import Config
from .errors import PipelineError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class JobStore:
    def __init__(self, job: Path):
        self.path = job.resolve()

    @classmethod
    def create(cls, cfg: Config) -> "JobStore":
        path = Path(cfg.output_dir).resolve() / str(uuid.uuid4())
        path.mkdir(parents=True)
        write_json(path / "config.json", cfg.model_dump())
        write_json(path / "job.json", {"version": 2, "created_at": now(),
            "providers": cfg.providers.model_dump(), "status": "created", "task_id": None, "candidates": []})
        return cls(path)

    @property
    def config(self) -> Config:
        if read_json(self.path / "job.json").get("version") != 2:
            raise PipelineError("Old job schema: migrate to a NEW job with migrate-v2; do not resume in place")
        return Config.model_validate(read_json(self.path / "config.json"))

    def lock(self, branch: int | None = None, timeout: float = 30) -> FileLock:
        directory = self.path if branch is None else self.branch(branch)
        return FileLock(str(directory / ".stage.lock"), timeout=timeout)

    def branch(self, index: int) -> Path:
        if type(index) is not int or not 0 <= index <= 100:
            raise ValueError("Invalid candidate index")
        p = self.path / f"v{index}"
        p.mkdir(exist_ok=True)
        return p

    def update(self, *, branch: int | None = None, **changes) -> dict:
        # Caller must hold lock(branch). Separate status files isolate candidates.
        directory = self.path if branch is None else self.branch(branch)
        target = directory / ("job.json" if branch is None else "branch.json")
        state = read_json(target) if target.exists() else {"created_at": now()}
        state.update(changes, updated_at=now())
        write_json(target, state)
        return state

    def status(self) -> dict:
        job = read_json(self.path / "job.json")
        branches = []
        for index, _ in enumerate(job.get("candidates", [])):
            p = self.path / f"v{index}" / "branch.json"
            branches.append({"index": index, **(read_json(p) if p.exists() else {"status": "pending"})})
        states = [x["status"] for x in branches]
        terminal = {"rendered", "rejected", "failed", "render_failed", "alignment_uncertain"}
        status = job["status"]
        if states and all(x == "rendered" for x in states):
            status = "completed"
        elif states and all(x in terminal for x in states):
            status = "partial" if "rendered" in states else "failed"
        return {"job": str(self.path), "status": status, "state": job, "branches": branches}
