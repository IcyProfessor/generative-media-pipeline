"""Install a built wheel in an isolated target and render outside the source tree.

Dependencies come from the current environment. This is NOT a clean dependency
resolution test; CI separately installs and checks declared dependencies.
"""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    wheels = list(args.wheel_dir.resolve().glob("genmedia_pipeline-*.whl"))
    if len(wheels) != 1:
        parser.error("Expected exactly one genmedia wheel in --wheel-dir")
    with tempfile.TemporaryDirectory(prefix="gmp-wheel-") as temp:
        root = Path(temp)
        target, work = root / "installed", root / "unrelated-working-directory"
        work.mkdir()
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
                        "--target", str(target), str(wheels[0])], check=True)
        env = {**os.environ, "PYTHONPATH": str(target), "PYTHONNOUSERSITE": "1"}
        for recipe in ("alphabet", "counting"):
            subprocess.run([sys.executable, "-m", "genmedia", "demo", "--recipe", recipe,
                            "--output", str(work / recipe)], cwd=work, env=env, check=True, timeout=300)
            outputs = list((work / recipe).glob("*/v*/render_result.json"))
            if len(outputs) != 2 or not all(json.loads(x.read_text())["decode_verified"] for x in outputs):
                raise RuntimeError("Expected two decoded video outputs per recipe")
        subprocess.run([sys.executable, "-c", "import genmedia; print('Wheel import:', genmedia.__file__)"],
                       cwd=work, env=env, check=True)
    print("PASS: wheel package data + four encoded/decoded MP4s outside checkout; dependencies inherited")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
