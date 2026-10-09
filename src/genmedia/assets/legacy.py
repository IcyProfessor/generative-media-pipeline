"""Import metadata from the original TXT catalog; never copy or delete media."""
from pathlib import Path, PureWindowsPath
import re
from ..models import Manifest, Asset


def import_catalog(source: Path, asset_root: Path, old_root: str) -> tuple[Manifest, list[str]]:
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    label, backgrounds = "", False
    assets, warnings, ids = [], [], set()
    for i, line in enumerate(lines):
        if re.fullmatch(r"[A-Z]", line.strip()):
            label = line.strip()
        if line.strip().lower() == "backgrounds:":
            backgrounds = True
        match = re.search(r'"([A-Za-z]:[\\/][^"\n]+\.(?:gif|png|jpe?g|webp))"', line, re.I)
        if not match:
            continue
        original = PureWindowsPath(match[1])
        try:
            relative = original.relative_to(PureWindowsPath(old_root)).as_posix()
        except ValueError:
            warnings.append(f"Skipped path outside old_root at line {i+1}")
            continue
        kind = "background" if backgrounds else "character"
        key = re.sub(r"[^a-z0-9_-]", "_", original.stem.lower()).strip("_")
        key = ("bg_" if backgrounds else label.lower()+"_") + key
        if key in ids:
            warnings.append(f"Duplicate identifier skipped: {key}")
            continue
        ids.add(key)
        # Some old background records had metadata BEFORE their path.
        around = lines[max(0, i-1):i+3]
        meta = next((x.strip() for x in around if x.strip().startswith("setting:" if backgrounds else "object:")), "")
        tags = [x.strip() for x in meta.split("|") if x.strip()]
        object_match = re.search(r"object:\s*([^|]+)", meta)
        obj = object_match[1].split(",")[0].split("(")[0].strip() if object_match else ""
        if not backgrounds and (not obj or not obj.upper().startswith(label)):
            warnings.append(f"Review object/letter association: {key} -> {obj!r}")
        assets.append(Asset(id=key, kind=kind, path=relative, label=None if backgrounds else label,
            object=obj, description=meta, tags=tags,
            provenance="Legacy user-generated background" if backgrounds else "Legacy downloaded GIF; redistribution permission unverified"))
    if not assets:
        raise ValueError("No assets parsed; check the catalog format and --old-root")
    return Manifest(root=str(asset_root.resolve()), assets=assets), warnings
