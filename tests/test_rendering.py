import os
import subprocess
import sys
from pathlib import Path
import pytest
from PIL import Image, ImageDraw
from genmedia.rendering.renderer import Sprite, Canvas
from genmedia.pipeline.timeline import subtitles_from_spans


def test_animated_gif_transparency_and_timing(tmp_path):
    frames=[]
    for x in (2,12):
        im=Image.new("RGBA",(32,32),(0,0,0,0))
        ImageDraw.Draw(im).rectangle((x,8,x+8,16),fill=(255,0,0,255))
        frames.append(im)
    path=tmp_path/"own.gif"
    frames[0].save(path,save_all=True,append_images=frames[1:],duration=[100,300],loop=0,disposal=2)
    sprite=Sprite(path,(32,32))
    assert len(sprite.ends)==2
    assert sprite.frame(.05).getpixel((3,10))[3]>0
    assert sprite.frame(.15).getpixel((3,10))[3]==0
    assert sprite.frame(.15).getpixel((13,10))[3]>0
    assert sprite.frame(.45).getpixel((3,10))[3]>0


def test_canvas_rgb_size_and_determinism(cfg,catalog,plan,transcript):
    import numpy as np
    canvas=Canvas(plan,catalog,subtitles_from_spans(transcript.spans),cfg.render)
    frame=canvas.frame(4.2)
    assert frame.shape==(cfg.render.height,cfg.render.width,3)
    assert frame.dtype==np.uint8
    assert np.array_equal(frame,canvas.frame(4.2))


def test_frame_at_every_scene_boundary(cfg,catalog,plan,transcript):
    canvas=Canvas(plan,catalog,subtitles_from_spans(transcript.spans),cfg.render)
    for scene in plan.scenes:
        for t in (scene.start,(scene.start+scene.end)/2,scene.end):
            assert canvas.frame(t).shape==(cfg.render.height,cfg.render.width,3)


def test_renderer_knows_no_domain_labels():
    import inspect
    from genmedia.rendering import renderer
    source=inspect.getsource(renderer)
    assert "INTRO" not in source and "OUTRO" not in source
    assert "int(scene.id)" not in source and "scene.label" not in source


def test_render_timeout_kills_subprocess_tree(tmp_path,monkeypatch):
    import genmedia.rendering.process as process
    (tmp_path/"v0").mkdir()
    original=subprocess.Popen
    spawned=[]
    def launch(command,**kwargs):
        p=original([sys.executable,"-c","import time; time.sleep(60)"],**kwargs)
        spawned.append(p)
        return p
    monkeypatch.setattr(process.subprocess,"Popen",launch)
    # taskkill internally uses Popen, so this precise injection test is POSIX-only.
    if os.name=="nt": pytest.skip("POSIX process-group assertion; Windows needs the native smoke")
    with pytest.raises(subprocess.TimeoutExpired):
        process.run_render(tmp_path,0,.1)
    assert len(spawned)==1 and spawned[0].poll() is not None


def test_render_subprocess_does_not_inherit_provider_credentials(tmp_path, monkeypatch):
    import genmedia.rendering.process as process
    (tmp_path/"v0").mkdir()
    for key in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "SUNO_API_KEY", "PRIVATE_TOKEN", "REDIS_URL"):
        monkeypatch.setenv(key, "test-sentinel")
    captured = {}
    class Done:
        def wait(self, timeout=None): return 0
    def launch(command, **kwargs):
        captured.update(kwargs)
        return Done()
    monkeypatch.setattr(process.subprocess, "Popen", launch)
    process.run_render(tmp_path, 0, 10)
    assert "PATH" in captured["env"]
    assert not any(k.endswith(("_API_KEY", "_API_KEYS", "_TOKEN")) or k == "REDIS_URL" for k in captured["env"])
