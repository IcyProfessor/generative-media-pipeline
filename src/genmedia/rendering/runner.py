from pathlib import Path
import argparse
from ..assets.manifest import Catalog
from ..models import ScenePlan
from ..pipeline.timeline import parse_srt
from ..store import JobStore, read_json
from .renderer import render


def main():
    parser = argparse.ArgumentParser(description="Isolated video render subprocess")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--branch", type=int, required=True)
    args = parser.parse_args()
    store = JobStore(args.job)
    cfg = store.config
    branch = store.branch(args.branch)
    state = read_json(branch / "branch.json")
    filename = state["audio_filename"]
    if Path(filename).name != filename:
        raise ValueError("Audio filename must not contain directory components")
    render(ScenePlan.model_validate_json((branch / "scene_plan.json").read_text(encoding="utf-8")),
           Catalog(Path(cfg.manifest)), parse_srt((branch / "subtitles.srt").read_text(encoding="utf-8")),
           branch / filename, branch / "final.mp4", cfg.render)

if __name__ == "__main__":
    main()
