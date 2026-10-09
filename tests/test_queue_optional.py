"""Runs only when the queue extra is installed; does not need an external Redis."""
from pathlib import Path
import importlib
import pytest

@pytest.mark.integration
def test_dramatiq_fanout_and_render_routing(cfg, monkeypatch):
    dramatiq = pytest.importorskip("dramatiq", reason="Install .[queue] to run the real Dramatiq StubBroker test")
    pytest.importorskip("redis")
    from dramatiq.brokers.stub import StubBroker
    import dramatiq.brokers.redis
    from genmedia.pipeline.orchestrator import create_job, Pipeline
    broker = StubBroker()
    monkeypatch.setattr(dramatiq.brokers.redis,"RedisBroker",lambda **_:broker)
    tasks = importlib.import_module("genmedia.tasks")
    rendered = []
    def render(self,index,**kwargs):
        rendered.append(index)
        return True
    monkeypatch.setattr(Pipeline,"render",render)
    store = create_job(cfg)
    worker = dramatiq.Worker(broker,worker_threads=2,worker_timeout=100)
    worker.start()
    try:
        tasks.start_job.send(str(store.path))
        broker.join("default", timeout=30000)
        worker.join()
        broker.join("rendering", timeout=30000)
        worker.join()
        assert sorted(rendered) == [0,1]
        assert tasks.render_branch.queue_name == "rendering"
    finally:
        worker.stop()
        broker.close()
