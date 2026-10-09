"""Isolated renderer with file-backed live output and bounded shutdown.

The child writes to a file, not a pipe. A broken console or a descendant that
inherits stdout cannot fill a pipe or make a reader-thread join hang forever.
"""
import codecs
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from ..errors import PipelineError
from ..security import render_environment


def _stop_tree(process):
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_render(job: Path, branch: int, timeout: int):
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise PipelineError("Render timeout must be a positive finite number")
    log = job / f"v{branch}" / "render.log"
    command = [sys.executable, "-m", "genmedia.rendering.runner",
               "--job", str(job), "--branch", str(branch)]
    console_ok = True
    try:
        print(f"Rendering v{branch}; live progress below; full log: {log}", flush=True)
    except (OSError, ValueError):
        console_ok = False
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    env = render_environment(os.environ)
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def display(chunk, *, final=False):
        nonlocal console_ok
        text = decoder.decode(chunk, final=final)
        if console_ok and text:
            try:
                sys.stdout.write(text)
                sys.stdout.flush()
            except (OSError, ValueError):
                console_ok = False

    with log.open("wb") as stream, log.open("rb") as reader:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                   env=env, **options)
        started = time.monotonic()
        try:
            while True:
                # Bounded reads keep a very chatty child from defeating timeout.
                display(reader.read(65536))
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    result = process.wait(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            # Snapshot the final file length; do not wait for inherited handles.
            remaining_bytes = os.fstat(reader.fileno()).st_size - reader.tell()
            while remaining_bytes > 0:
                chunk = reader.read(min(65536, remaining_bytes))
                if not chunk:
                    break
                display(chunk)
                remaining_bytes -= len(chunk)
            display(b"", final=True)
        except BaseException:
            _stop_tree(process)
            raise
    if result:
        raise PipelineError(f"Render subprocess exited {result}; see render.log")
