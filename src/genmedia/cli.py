from __future__ import annotations
import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv
from . import __version__
from .config import load_config, builtin_config, apply_live_env
from .models import ProviderSpec, Script
from .errors import PipelineError
from .security import redact
from .store import JobStore, write_json, read_json
from .pipeline.orchestrator import create_job, Pipeline


def display_status(store: JobStore) -> int:
    result = store.status()
    branches = result["branches"]
    view = {"job": result["job"], "phase": result["status"], "branches": branches,
            "rendered": sum(x["status"] == "rendered" for x in branches)}
    print(json.dumps(view, indent=2, ensure_ascii=False))
    return 0 if branches and all(x["status"] == "rendered" for x in branches) else 2


def command_config(args, *, live=False):
    if args.config is not None:
        if getattr(args, "recipe", None):
            raise PipelineError("Use either --config or --recipe, not both")
        cfg = load_config(args.config, live=live, output=getattr(args, "output", None))
    else:
        cfg = builtin_config(getattr(args, "recipe", None) or "alphabet", getattr(args, "output", None))
        if live:
            cfg.providers = ProviderSpec(text="gemini", audio="suno", alignment="suno")
            apply_live_env(cfg)
    return cfg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Recipe-based generative media pipeline")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "run", "submit"):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path)
        p.add_argument("--recipe", choices=("alphabet", "counting"))
        p.add_argument("--output", type=Path)
        if name != "demo":
            p.add_argument("--env-file", type=Path)
            p.add_argument("--live", action="store_true")
            p.add_argument("--allow-paid", action="store_true")
    for name in ("status", "resume", "replay", "attach-task"):
        p = sub.add_parser(name)
        p.add_argument("--job", type=Path, required=True)
        if name == "resume":
            p.add_argument("--env-file", type=Path)
            p.add_argument("--allow-paid", action="store_true")
            p.add_argument("--queue", action="store_true")
            p.add_argument("--extend-poll", action="store_true")
            p.add_argument("--retry-alignment", action="store_true", help="Explicitly permit another possibly charged alignment")
        if name == "replay":
            p.add_argument("--rebuild-plan", action="store_true")
            p.add_argument("--force-render", action="store_true")
        if name == "attach-task":
            p.add_argument("--task-id", required=True)
    p = sub.add_parser("worker")
    p.add_argument("queue", choices=("default", "rendering"))
    p.add_argument("--processes", type=int, default=1)
    p.add_argument("--threads", type=int)
    p.add_argument("--env-file", type=Path)
    p = sub.add_parser("import-legacy")
    p.add_argument("--catalog", type=Path, required=True)
    p.add_argument("--asset-root", type=Path, required=True)
    p.add_argument("--old-root", required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("smoke-gemini")
    p.add_argument("--config", type=Path)
    p.add_argument("--recipe", choices=("alphabet", "counting"))
    p.add_argument("--env-file", type=Path, required=True)
    p.add_argument("--allow-paid", action="store_true")
    p = sub.add_parser("list-models")
    p.add_argument("--env-file", type=Path, required=True)
    p = sub.add_parser("replay-legacy")
    p.add_argument("--job", type=Path, required=True)
    p.add_argument("--branch", choices=("v0", "v1"), default="v0")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--max-tail", type=float, default=5.0)
    p = sub.add_parser("migrate-v2")
    p.add_argument("--job", type=Path, required=True)
    p.add_argument("--output", type=Path, default=Path("output"))
    p.add_argument("--manifest", type=Path)
    sub.add_parser("doctor")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for logger_name in ("httpx", "httpcore", "google.genai"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    if getattr(args, "env_file", None):
        if not args.env_file.is_file():
            parser.error("The explicit --env-file does not exist")
        load_dotenv(args.env_file.resolve(), override=False)
    try:
        if args.command == "migrate-v2":
            from .pipeline.migration import migrate_v2
            store = migrate_v2(args.job, args.output, args.manifest)
            print(f"New replay-only job: {store.path}")
            print("Original unchanged. Run replay --rebuild-plan on this NEW job.")
            return 0
        if args.command == "replay-legacy":
            import uuid
            from filelock import FileLock
            from .assets.manifest import Catalog
            from .media import audio_duration
            from .pipeline.legacy_replay import convert_plan
            from .pipeline.timeline import parse_srt
            from .rendering.renderer import render
            from .recipes import get_recipe
            cfg = load_config(args.config)
            recipe = get_recipe(cfg.recipe)
            if recipe.name != "alphabet":
                raise PipelineError("Original pre-v0.2 scene JSON supports only alphabet replay")
            catalog = Catalog(Path(cfg.manifest))
            audio = args.job / f"audio_{args.branch}.mp3"
            srt = args.job / f"audio_{args.branch}.srt"
            plan, adjustment = convert_plan(read_json(args.job/f"audio_{args.branch}.json"),
                catalog, recipe.options.labels, audio_duration(audio), args.max_tail)
            cues = parse_srt(srt.read_text(encoding="utf-8-sig"))
            output = Path(cfg.output_dir)/("legacy-"+str(uuid.uuid4()))
            output.mkdir(parents=True)
            write_json(output/"scene_plan.json", plan.model_dump())
            write_json(output/"replay.json", {"source": "legacy", "timing_policy": "preserved_from_legacy_json",
                "outro_adjustment_seconds": adjustment, "acoustic_correctness_checked": False})
            with FileLock(str(Path(cfg.output_dir)/".render.lock"), timeout=cfg.render.timeout_seconds):
                render(plan, catalog, cues, audio, output/"final.mp4", cfg.render)
            print(f"Legacy replay rendered: {output/'final.mp4'}")
            return 0
        if args.command == "doctor":
            from importlib.metadata import version, PackageNotFoundError
            from imageio_ffmpeg import get_ffmpeg_exe
            print(f"genmedia: {__version__}")
            print(f"Python: {sys.version.split()[0]}; declared range: >=3.12,<3.15")
            print(f"FFmpeg: {get_ffmpeg_exe()}")
            for name in ("pydantic", "moviepy", "Pillow", "google-genai", "dramatiq", "redis"):
                try:
                    print(f"{name}: {version(name)}")
                except PackageNotFoundError:
                    print(f"{name}: not installed (optional for live/queue)")
            if int(version("Pillow").split(".")[0]) >= 12:
                print("WARNING: MoviePy 2.2.1 declares Pillow<12. Reinstall from pyproject.toml in a clean venv.")
            return 0
        if args.command == "worker":
            threads = args.threads if args.threads is not None else (1 if args.queue == "rendering" else 2)
            if args.processes < 1 or threads < 1:
                parser.error("Worker counts must be positive")
            return subprocess.call([sys.executable, "-m", "dramatiq", "genmedia.tasks", "--queues", args.queue,
                                    "-p", str(args.processes), "-t", str(threads)])
        if args.command == "import-legacy":
            from .assets.legacy import import_catalog
            if args.out.exists():
                raise PipelineError("Refusing to overwrite an existing manifest")
            manifest, warnings = import_catalog(args.catalog, args.asset_root, args.old_root)
            write_json(args.out, manifest.model_dump())
            print(f"Imported {len(manifest.assets)} metadata records; no media copied")
            for warning in warnings:
                print("REVIEW:", warning)
            return 0
        if args.command in {"smoke-gemini", "list-models"}:
            from .providers.gemini import GeminiText
            if args.command == "list-models":
                provider = GeminiText(os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"))
                try:
                    for model in provider.client.models.list():
                        print(model.name)
                except Exception:
                    raise PipelineError("Gemini model listing failed; check credentials/network/account") from None
                finally:
                    provider.close()
                return 0
            if not args.allow_paid:
                parser.error("Generation can incur charges; add --allow-paid after checking quota")
            from .assets.manifest import Catalog
            from .recipes import get_recipe
            cfg = command_config(args, live=True)
            recipe, catalog = get_recipe(cfg.recipe), Catalog(Path(cfg.manifest))
            recipe.validate_catalog(catalog)
            provider = GeminiText(cfg.gemini.model)
            try:
                script = provider.generate(recipe.script_prompt(catalog, cfg.seed), Script)
                recipe.validate_script(script, catalog)
                print(script.model_dump_json(indent=2))
            finally:
                provider.close()
            return 0
        if args.command in {"demo", "run", "submit"}:
            cfg = command_config(args, live=getattr(args, "live", False))
            if args.command == "demo":
                cfg.providers = ProviderSpec()
            if cfg.providers.needs_paid_ack and not getattr(args, "allow_paid", False):
                parser.error("External generation requires --allow-paid; demo/replay need no keys")
            store = create_job(cfg)
            print(f"Job created: {store.path}", flush=True)
            if args.command == "submit":
                from .tasks import start_job
                start_job.send(str(store.path))
                print("Enqueued. Run both default and rendering workers.")
                return 0
            with Pipeline(store) as pipeline:
                pipeline.run_local()
            return display_status(store)
        store = JobStore(args.job)
        if args.command == "status":
            display_status(store)
            return 0
        with Pipeline(store) as pipeline:
            if args.command == "attach-task":
                pipeline.attach_task(args.task_id)
                print("Task ID attached. No generation request sent.")
                return 0
            if args.command == "replay":
                for branch in store.status()["branches"]:
                    index = branch["index"]
                    if pipeline.prepare(index, offline=True, rebuild=args.rebuild_plan):
                        pipeline.render(index, force=args.force_render)
            elif args.command == "resume":
                if (pipeline.cfg.providers.needs_paid_ack or args.retry_alignment) and not args.allow_paid:
                    parser.error("Resume may call paid endpoints; add --allow-paid")
                if args.extend_poll:
                    import time
                    with store.lock():
                        store.update(poll_deadline=time.time()+pipeline.cfg.audio.poll_timeout_seconds)
                if args.queue:
                    if args.retry_alignment:
                        parser.error("Retry an uncertain charged alignment locally, not through redeliverable queue messages")
                    from .tasks import start_job
                    start_job.send(str(store.path))
                    print("Resume enqueued; checkpoints will be reused")
                    return 0
                pipeline.run_local(retry_alignment=args.retry_alignment)
        return display_status(store)
    except (PipelineError, ValueError, FileNotFoundError, ImportError) as exc:
        print(f"ERROR: {redact(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; saved checkpoints remain available", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
