"""Thin queue adapters. Workers share the same local filesystem and output root.

Do not expose Redis publicly. Messages contain job paths, never API keys.
"""
import os
import dramatiq
from dramatiq.brokers.redis import RedisBroker
from .store import JobStore
from .pipeline.orchestrator import Pipeline
from pathlib import Path

broker = RedisBroker(url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                     namespace=os.environ.get("GENMEDIA_QUEUE_NAMESPACE", "genmedia"))
dramatiq.set_broker(broker)


@dramatiq.actor(max_retries=0, time_limit=180000)
def start_job(job_path: str):
    with Pipeline(JobStore(Path(job_path))) as pipeline:
        pipeline.start()
    poll_audio.send(job_path)


@dramatiq.actor(max_retries=0, time_limit=180000)
def poll_audio(job_path: str):
    with Pipeline(JobStore(Path(job_path))) as pipeline:
        branches = pipeline.poll()
        if branches is None:
            poll_audio.send_with_options(args=(job_path,), delay=int(pipeline.cfg.audio.poll_interval_seconds * 1000))
            return
    for index in branches:
        prepare_branch.send(job_path, index)


@dramatiq.actor(max_retries=0, time_limit=600000)
def prepare_branch(job_path: str, index: int):
    with Pipeline(JobStore(Path(job_path))) as pipeline:
        if pipeline.prepare(index):
            render_branch.send(job_path, index)


@dramatiq.actor(queue_name="rendering", max_retries=0, time_limit=86400000)
def render_branch(job_path: str, index: int):
    with Pipeline(JobStore(Path(job_path))) as pipeline:
        pipeline.render(index)
