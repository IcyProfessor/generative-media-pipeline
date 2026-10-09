"""Opt-in real Redis/Dramatiq integration, synthetic providers only.

Starts actual default/rendering worker subprocesses on a unique Redis namespace.
Never flushes Redis or touches other queues. Requires .[queue] and a private Redis.
Residual test keys may retain the printed namespace prefix until Redis cleanup.
"""
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid


def stop(process):
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()


def main() -> int:
    import redis
    from genmedia.config import builtin_config
    from genmedia.pipeline.orchestrator import create_job
    from genmedia.media import verify_video

    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
    try:
        client.ping()
    finally:
        client.close()
    namespace = "genmedia-smoke-" + uuid.uuid4().hex
    os.environ["GENMEDIA_QUEUE_NAMESPACE"] = namespace
    from genmedia import tasks
    print("Isolated queue namespace:", namespace, flush=True)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    workers, streams = [], []
    # Under ignored output/, retain failed-job state and worker logs for diagnosis.
    root = Path("output") / namespace
    root.mkdir(parents=True)
    try:
        for queue, threads in (("default", 2), ("rendering", 1)):
            stream = (root / f"{queue}.log").open("w", encoding="utf-8")
            streams.append(stream)
            workers.append(subprocess.Popen([sys.executable, "-m", "genmedia", "worker", queue,
                "--processes", "1", "--threads", str(threads)], stdout=stream, stderr=subprocess.STDOUT,
                env=dict(os.environ), **options))
        cfg = builtin_config("counting", root)
        cfg.render.width, cfg.render.height, cfg.render.fps = 320, 180, 6
        store = create_job(cfg)
        tasks.start_job.send(str(store.path))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in workers):
                raise RuntimeError(f"Worker exited early; inspect {root}")
            state = store.status()
            if state["status"] == "completed":
                for index in (0, 1):
                    verify_video(store.path / f"v{index}" / "final.mp4")
                print(json.dumps({"status": "PASS", "job": str(store.path), "videos": 2}, indent=2))
                return 0
            if state["status"] in {"failed", "partial"}:
                raise RuntimeError(f"Queue job failed; inspect {store.path}")
            time.sleep(.5)
        raise TimeoutError(f"Queue smoke timed out; inspect {root}")
    finally:
        for worker in reversed(workers):
            stop(worker)
        for stream in streams:
            stream.close()
        tasks.broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
