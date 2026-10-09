"""The same stages power local execution and thin queue actors.

The engine knows no letter/number anchors, Suno response keys or visual layouts.
Filesystem locks implement single-host coordination, not distributed exactly-once
transactions. Ambiguous paid operations require explicit reconciliation.
"""
from __future__ import annotations
from contextlib import nullcontext
from hashlib import sha256
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
import json
import logging
import os
import platform
import time
from filelock import FileLock
from .. import __version__
from ..assets.manifest import Catalog
from ..models import Config, Script, Candidate, Transcript, VisualPlan, ScenePlan
from ..errors import PipelineError, QualityError, SubmissionRejected, SubmissionUncertain, AlignmentUncertain
from ..security import redact
from ..media import audio_duration
from ..store import JobStore, write_json, write_text, read_json
from ..recipes import get_recipe
from ..providers import factory
from ..providers.base import AlignmentRequest
from .timeline import validate_transcript, subtitles_from_spans, format_srt
from .validation import validate_plan

log = logging.getLogger(__name__)


def digest(path: Path) -> str:
    checksum = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def seal_snapshot(store: JobStore) -> None:
    write_json(store.path / "snapshot_hashes.json", {name: digest(store.path / name)
        for name in ("config.json", "asset_manifest.json", "asset_hashes.json", "recipe.json")})


def create_job(cfg: Config, *, recipe=None) -> JobStore:
    cfg = cfg.model_copy(deep=True)
    factory.validate_selection(cfg)
    recipe = recipe or get_recipe(cfg.recipe)
    if recipe.name != cfg.recipe.name:
        raise PipelineError("Injected recipe does not match configuration")
    catalog = Catalog(Path(cfg.manifest))
    recipe.validate_catalog(catalog)
    if cfg.render.font_path and not Path(cfg.render.font_path).is_file():
        raise QualityError("Configured font file does not exist")
    # Hash and validate before creating a half-initialized job directory.
    hashes = {key: digest(catalog.resolve(key)) for key in catalog.by_id}
    store = JobStore.create(cfg)
    snapshot = catalog.manifest.model_dump()
    snapshot["root"] = str(catalog.root)
    write_json(store.path / "asset_manifest.json", snapshot)
    cfg.manifest = str(store.path / "asset_manifest.json")
    cfg.output_dir = str(Path(cfg.output_dir).resolve())
    write_json(store.path / "config.json", cfg.model_dump())
    write_json(store.path / "asset_hashes.json", hashes)
    write_json(store.path / "recipe.json", {"name": recipe.name, "version": recipe.version})
    if cfg.render.font_path:
        write_json(store.path / "font_hash.json", {"sha256": digest(Path(cfg.render.font_path))})
    seal_snapshot(store)
    packages = {}
    for name in ("pydantic", "moviepy", "Pillow", "numpy", "google-genai", "dramatiq", "httpx"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    write_json(store.path / "environment.json", {"python": platform.python_version(),
        "platform": platform.platform(), "packages": packages, "pipeline_version": __version__})
    return store


class Pipeline:
    def __init__(self, store: JobStore, text_provider=None, audio_provider=None,
                 timestamp_provider=None, render_runner=None, *, recipe=None):
        self.store, self.cfg = store, store.config
        self.catalog = Catalog(Path(self.cfg.manifest))
        self.recipe = recipe or get_recipe(self.cfg.recipe)
        recorded = read_json(store.path / "recipe.json")
        if recorded != {"name": self.recipe.name, "version": self.recipe.version}:
            raise PipelineError("Recipe version changed; explicit migration is required")
        self._text, self._audio, self._timestamp = text_provider, audio_provider, timestamp_provider
        self._render_runner = render_runner
        self._check_snapshot()

    @property
    def script(self) -> Script:
        return Script.model_validate(read_json(self.store.path / "script.json"))

    @property
    def text(self):
        if self._text is None:
            self._text = factory.text_provider(self.cfg, self.recipe, self.catalog)
        return self._text

    @property
    def audio(self):
        if self._audio is None:
            script = self.script if (self.store.path / "script.json").exists() else None
            self._audio = factory.audio_provider(self.cfg, script)
        return self._audio

    @property
    def timestamp_provider(self):
        if self._timestamp is None:
            self._timestamp = factory.alignment_provider(self.cfg)
        return self._timestamp

    def _check_snapshot(self):
        expected = read_json(self.store.path / "snapshot_hashes.json")
        if set(expected) != {"config.json", "asset_manifest.json", "asset_hashes.json", "recipe.json"}:
            raise QualityError("Invalid job snapshot receipt")
        for name, fingerprint in expected.items():
            if digest(self.store.path / name) != fingerprint:
                raise QualityError(f"Job snapshot changed: {name}; create a new job, do not edit checkpoints")

    def _check_assets(self):
        self._check_snapshot()
        expected = read_json(self.store.path / "asset_hashes.json")
        for key, fingerprint in expected.items():
            if digest(self.catalog.resolve(key)) != fingerprint:
                raise QualityError(f"Asset changed since job creation: {key}")
        if self.cfg.render.font_path:
            if digest(Path(self.cfg.render.font_path)) != read_json(self.store.path / "font_hash.json")["sha256"]:
                raise QualityError("Configured font changed since job creation")

    def _check_script(self):
        state = read_json(self.store.path / "job.json")
        if state.get("script_sha256") and digest(self.store.path / "script.json") != state["script_sha256"]:
            raise QualityError("Saved script changed after generation")
        self.recipe.validate_script(self.script, self.catalog)

    def start(self) -> str:
        with self.store.lock(timeout=180):
            self._check_assets()
            state = read_json(self.store.path / "job.json")
            if state.get("offline_only"):
                raise PipelineError("Migrated job is replay-only; no provider calls are allowed")
            if state.get("task_id"):
                self._check_script()
                return state["task_id"]
            if state["status"] in {"submitting", "submission_uncertain"}:
                raise SubmissionUncertain("Check provider dashboard and use attach-task; automatic resubmission is blocked")
            # Credential/configuration failures should precede text generation.
            self.audio.preflight_submit()
            path = self.store.path / "script.json"
            if not path.exists():
                log.info("Generating script: %s", self.store.path.name)
                script = self.text.generate(self.recipe.script_prompt(self.catalog, self.cfg.seed), Script)
                script = Script.model_validate(script)
                self.recipe.validate_script(script, self.catalog)
                write_json(path, script.model_dump())
                write_text(self.store.path / "script.txt", script.plain_text())
                write_json(self.store.path / "script_usage.json", getattr(self.text, "last_usage", None))
            self._check_script()
            self.store.update(script_sha256=digest(path))

            # This marker is committed BEFORE making a possibly paid POST.
            self.store.update(status="submitting", submitted_at=time.time())

            try:
                task_id = self.audio.submit(self.script, self.cfg.audio)

                if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
                    raise SubmissionUncertain(
                        "Audio provider reported success but returned an invalid task ID"
                    )

            except SubmissionUncertain:
                self.store.update(
                    status="submission_uncertain",
                    error="Submission may have reached provider; reconcile task ID before retrying",
                )
                raise

            except SubmissionRejected:
                self.store.update(
                    status="submission_rejected",
                    error="Audio submission was explicitly refused or not sent",
                )
                raise

            except Exception:
                self.store.update(
                    status="submission_uncertain",
                    error="Audio submit failed without proof of refusal; reconcile before retrying",
                )
                raise SubmissionUncertain(
                    "Audio submit outcome is unknown; check the provider dashboard before retrying"
                ) from None

            self.store.update(
                status="audio_pending",
                task_id=task_id,
                poll_deadline=time.time() + self.cfg.audio.poll_timeout_seconds,
                error=None,
            )

            return task_id


    def attach_task(self, task_id: str):
        if not task_id.strip() or len(task_id) > 200:
            raise PipelineError("Invalid task ID")
        with self.store.lock():
            self._check_assets()
            state = read_json(self.store.path / "job.json")
            if state.get("offline_only") or state.get("candidates") or state.get("task_id"):
                raise PipelineError("This job already has a task or is replay-only; refusing replacement")
            if not (self.store.path / "script.json").exists():
                raise PipelineError("Cannot attach a task without its corresponding saved script")
            self._check_script()
            self.store.update(status="audio_pending", task_id=task_id,
                script_sha256=digest(self.store.path / "script.json"),
                poll_deadline=time.time()+self.cfg.audio.poll_timeout_seconds, error=None)

    def poll(self) -> list[int] | None:
        with self.store.lock(timeout=180):
            self._check_assets()
            state = read_json(self.store.path / "job.json")
            if state.get("candidates"):
                candidates = [Candidate.model_validate(x) for x in state["candidates"]]
                self._ensure_branches(candidates)
                return list(range(len(candidates)))
            if state.get("offline_only"):
                raise PipelineError("Offline-only job has no saved candidates")
            if not state.get("task_id"):
                raise PipelineError("No audio task was submitted")
            if time.time() > state["poll_deadline"]:
                self.store.update(status="poll_timeout", error="Polling deadline reached; inspect dashboard")
                raise PipelineError("Polling deadline reached; no paid task was resubmitted")
            try:
                candidates = self.audio.poll(state["task_id"])
                if candidates is not None:
                    candidates = [Candidate.model_validate(x) for x in candidates]
                    if not candidates or len(candidates) > 16 or len({x.id for x in candidates}) != len(candidates):
                        raise PipelineError("Empty, duplicate or excessive audio candidates")
            except Exception as exc:
                self.store.update(status="poll_failed", error=self._safe_error(exc))
                raise
            if candidates is None:
                return None
            self.store.update(status="processing_variants", candidates=[x.model_dump() for x in candidates], error=None)
            self._ensure_branches(candidates)
            return list(range(len(candidates)))

    def _ensure_branches(self, candidates):
        # Recover a crash between committing candidates and writing branch files.
        for index, candidate in enumerate(candidates):
            if (self.store.branch(index) / "branch.json").exists():
                continue
            with self.store.lock(index):
                if not (self.store.branch(index) / "branch.json").exists():
                    self.store.update(branch=index, status="pending", candidate_id=candidate.id,
                        audio_filename=f"audio.{candidate.extension}")

    @staticmethod
    def _safe_error(exc):
        return redact(exc) if isinstance(exc, PipelineError) else f"Stage failed ({type(exc).__name__})"

    def _transcript(self, index, candidate, audio_path, *, offline, retry_alignment):
        directory = self.store.branch(index)
        transcript_path = directory / "transcript.json"
        if transcript_path.exists():
            return Transcript.model_validate(read_json(transcript_path))
        if offline:
            raise PipelineError("Offline replay needs transcript.json; no aligner was called")
        attempt_path = directory / "alignment_attempt.json"
        previous = read_json(attempt_path) if attempt_path.exists() else {}
        if previous.get("billable") and previous.get("status") in {"started", "uncertain", "saved"} and not retry_alignment:
            raise AlignmentUncertain("Alignment may have been charged; use resume --retry-alignment --allow-paid explicitly")
        provider = self.timestamp_provider
        billable = bool(getattr(provider, "billable", True))
        attempt = {"billable": billable, "status": "started", "attempt": previous.get("attempt", 0)+1}
        write_json(attempt_path, attempt)
        try:
            task = read_json(self.store.path / "job.json")["task_id"]
            transcript = Transcript.model_validate(provider.align(AlignmentRequest(task, candidate, audio_path, self.script)))
            write_json(transcript_path, transcript.model_dump())
            write_json(attempt_path, {**attempt, "status": "saved", "sha256": digest(transcript_path)})
            return transcript
        except Exception as exc:
            write_json(attempt_path, {**attempt, "status": "uncertain",
                "error_type": type(exc).__name__, "error": self._safe_error(exc)})
            if billable:
                raise AlignmentUncertain("Alignment result not saved; possible charge. Explicit retry consent is required") from None
            raise

    def _prepared_files(self, index):
        directory = self.store.branch(index)
        candidate = Candidate.model_validate(read_json(self.store.path / "job.json")["candidates"][index])
        return {"script.json": self.store.path / "script.json", **{name: directory / name for name in
            (f"audio.{candidate.extension}", "transcript.json", "visuals.json", "timing.json", "subtitles.srt", "scene_plan.json")}}

    def _seal_prepared(self, index):
        values = {name: digest(path) for name, path in self._prepared_files(index).items()}
        write_json(self.store.branch(index) / "prepared_hashes.json", values)
        return values

    def _check_prepared(self, index):
        self._check_assets()
        self._check_script()
        expected = read_json(self.store.branch(index) / "prepared_hashes.json")
        actual = {name: digest(path) for name, path in self._prepared_files(index).items()}
        if actual != expected:
            raise QualityError("Prepared artifacts changed; use offline replay --rebuild-plan to revalidate, never trust stale MP4")
        plan = ScenePlan.model_validate(read_json(self.store.branch(index) / "scene_plan.json"))
        validate_plan(plan, self.catalog)
        if [s.id for s in plan.scenes] != [s.id for s in self.script.segments]:
            raise QualityError("Cached scene plan differs from script segment IDs")
        return sha256(json.dumps({"artifacts": actual, "render": self.cfg.render.model_dump(),
                                 "engine": __version__}, sort_keys=True).encode()).hexdigest()

    def prepare(self, index: int, *, offline=False, rebuild=False, retry_alignment=False) -> bool:
        with self.store.lock(index, timeout=600):
            directory = self.store.branch(index)
            state = read_json(directory / "branch.json")
            if not rebuild and state.get("status") == "rejected":
                return False
            stage = "cached_plan" if state.get("status") in {"prepared", "rendered"} else "audio"
            try:
                self._check_assets()
                self._check_script()
                if not rebuild and state.get("status") in {"prepared", "rendered"}:
                    self._check_prepared(index)
                    return True
                stage = "audio"
                self.store.update(branch=index, status="preparing", stage=stage, error=None)
                job = read_json(self.store.path / "job.json")
                offline = offline or job.get("offline_only", False)
                candidate = Candidate.model_validate(job["candidates"][index])
                audio_path = directory / f"audio.{candidate.extension}"
                self.store.update(branch=index, audio_filename=audio_path.name)
                if not audio_path.exists():
                    if offline:
                        raise PipelineError("Offline replay needs a saved audio file")
                    temp = directory / f"audio.part.{candidate.extension}"
                    try:
                        self.audio.download(candidate, temp)
                        audio_duration(temp)
                        os.replace(temp, audio_path)
                    finally:
                        temp.unlink(missing_ok=True)
                duration = audio_duration(audio_path)
                stage = "alignment"
                self.store.update(branch=index, stage=stage, duration=duration)
                transcript = self._transcript(index, candidate, audio_path, offline=offline, retry_alignment=retry_alignment)
                validate_transcript(transcript, duration)
                write_text(directory / "subtitles.srt", format_srt(subtitles_from_spans(transcript.spans)))
                stage = "validation"
                self.store.update(branch=index, stage=stage)
                timing = self.recipe.timeline(self.script, transcript, duration, self.catalog)
                write_json(directory / "timing.json", [x.model_dump() for x in timing])
                stage = "visual_planning"
                self.store.update(branch=index, stage=stage)
                visuals_path = directory / "visuals.json"
                if visuals_path.exists():
                    visuals = VisualPlan.model_validate(read_json(visuals_path))
                else:
                    if offline:
                        raise PipelineError("Offline replay needs visuals.json; no LLM was called")
                    prompt = self.recipe.visual_prompt(self.catalog, self.script, transcript)
                    visuals = VisualPlan.model_validate(self.text.generate(prompt, VisualPlan))
                    write_json(visuals_path, visuals.model_dump())
                    write_json(directory / "visuals_usage.json", getattr(self.text, "last_usage", None))
                plan = self.recipe.compile_plan(self.script, visuals, timing, self.catalog, duration)
                validate_plan(plan, self.catalog)
                if [s.id for s in plan.scenes] != [s.id for s in self.script.segments]:
                    raise QualityError("Compiled scene plan IDs differ from script")
                write_json(directory / "scene_plan.json", plan.model_dump())
                self._seal_prepared(index)
                write_json(directory / "validation.json", {"passed": True, "scope": "transcript_and_scene_contract",
                    "recipe": self.recipe.name, "recipe_version": self.recipe.version,
                    "acoustic_correctness_checked": False, "alignment_provider": transcript.provider})
                self.store.update(branch=index, status="prepared", stage="ready_to_render", error=None)
                log.info("Prepared v%s: %.2fs, %s scenes", index, duration, len(plan.scenes))
                return True
            except AlignmentUncertain as exc:
                self.store.update(branch=index, status="alignment_uncertain", stage=stage, error=str(exc))
                return False
            except QualityError as exc:
                write_json(directory / "validation.json", {"passed": False, "stage": stage, "reason": str(exc)})
                self.store.update(branch=index, status="rejected", stage=stage, error=str(exc))
                log.warning("Rejected v%s: %s", index, exc)
                return False
            except Exception as exc:
                self.store.update(branch=index, status="failed", stage=stage, error=self._safe_error(exc))
                log.error("Failed v%s at %s: %s", index, stage, self._safe_error(exc))
                return False

    def render(self, index: int, *, force=False) -> bool:
        from ..rendering.process import run_render
        from ..media import verify_video
        timeout = self.cfg.render.timeout_seconds
        with self.store.lock(index, timeout=timeout+60):
            directory = self.store.branch(index)
            state = read_json(directory / "branch.json")
            if state.get("status") not in {"prepared", "rendered", "render_failed", "rendering"}:
                return False
            output, result_path = directory / "final.mp4", directory / "render_result.json"
            host_lock = FileLock(str(Path(self.cfg.output_dir) / ".render.lock"), timeout=timeout+60)
            with host_lock if self.cfg.render.serial else nullcontext():
                started = time.perf_counter()
                try:
                    signature = self._check_prepared(index)
                    if not force and result_path.exists() and output.exists():
                        receipt = read_json(result_path)
                        if (receipt.get("input_signature") == signature and receipt.get("decode_verified") is True
                                and digest(output) == receipt.get("sha256")):
                            self.store.update(branch=index, status="rendered", stage="complete", error=None)
                            return True
                    self.store.update(branch=index, status="rendering", stage="render", error=None)
                    (self._render_runner or run_render)(self.store.path, index, timeout)
                    verify_video(output, timeout=timeout)
                    write_json(result_path, {"sha256": digest(output), "size_bytes": output.stat().st_size,
                        "input_signature": signature, "elapsed_seconds": time.perf_counter()-started,
                        "decode_verified": True})
                    self.store.update(branch=index, status="rendered", stage="complete", error=None)
                    return True
                except Exception as exc:
                    self.store.update(branch=index, status="render_failed", stage="render", error=self._safe_error(exc))
                    log.error("Render v%s failed: %s", index, self._safe_error(exc))
                    return False

    def run_local(self, *, retry_alignment=False):
        self.start()
        while (branches := self.poll()) is None:
            log.info("Audio pending; next poll in %.1fs", self.cfg.audio.poll_interval_seconds)
            time.sleep(self.cfg.audio.poll_interval_seconds)
        # Conservative local mode: sequential. Queue workers can prepare in parallel.
        for index in branches:
            if self.prepare(index, retry_alignment=retry_alignment):
                self.render(index)
        return self.store.status()

    def close(self):
        closed = set()
        for provider in (self._text, self._audio, self._timestamp):
            if provider is not None and id(provider) not in closed:
                provider.close()
                closed.add(id(provider))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
