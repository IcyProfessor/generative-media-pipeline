"""Export only publication candidates, then scan the staged tree before zipping.

This intentionally is not shutil.make_archive(checkout). No Git or API required.
Inspect the resulting ZIP and choose a code license before public distribution.
"""
import argparse
from pathlib import Path
import shutil
import tempfile
import zipfile

from check_release import check, IGNORED, FORBIDDEN_SUFFIXES

ROOT_FILES = {"README.md", "START_HERE.md", "CHANGELOG.md", "MANIFEST.in", "pyproject.toml",
              ".env.example", ".gitignore", ".gitattributes", ".python-version", "docker-compose.yml", "LICENSE"}
ROOT_DIRS = {"src", "tests", "docs", "configs", "scripts", "examples", ".github"}
CONFIGS = {"alphabet.yaml", "counting.yaml", "alphabet-full.example.yaml"}
SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".srt", ".wav", ".png", ".mp4", ".cmd"}


def publishable(path: Path) -> bool:
    if any(part in IGNORED or part.endswith(".egg-info") for part in path.parts):
        return False
    if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.startswith(".env") and path.name != ".env.example":
        return False
    if len(path.parts) == 1:
        return path.name in ROOT_FILES
    if path.parts[0] not in ROOT_DIRS or path.suffix.lower() not in SUFFIXES:
        return False
    if path.parts[0] == "configs":
        return len(path.parts) == 2 and path.name in CONFIGS
    if path.parts[0] == "src" and path.parts[1] != "genmedia":
        return False
    return True


def export(root: Path, output: Path) -> int:
    if output.exists():
        raise FileExistsError("Refusing to overwrite an existing archive")
    with tempfile.TemporaryDirectory(prefix="genmedia-public-") as temp:
        stage = Path(temp) / "generative-media-pipeline"
        stage.mkdir()
        for source in sorted(root.rglob("*")):
            relative = source.relative_to(root)
            if not publishable(relative):
                continue
            if source.is_symlink() or any(p.is_symlink() for p in source.parents if p != root):
                raise ValueError(f"Symlink is not a release input: {relative}")
            if source.is_file():
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        report = check(stage, strict=True)
        if report["findings"]:
            # Contains only paths/rule names, never the matched credentials.
            raise ValueError(f"Publication scan failed: {report['findings']}")
        if not (stage / "pyproject.toml").is_file():
            raise ValueError("Project root does not contain pyproject.toml")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage.parent).as_posix())
        return report["files_scanned"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    count = export(args.root.resolve(), args.out.resolve())
    print(f"Created {args.out}; {count} staged files passed the limited publication scan")
    print("Manual review of credentials, binary assets and license is still required")


if __name__ == "__main__":
    main()
