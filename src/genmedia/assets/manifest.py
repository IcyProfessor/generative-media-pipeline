from pathlib import Path, PurePosixPath, PureWindowsPath
from ..models import Manifest
from ..errors import QualityError


class Catalog:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.manifest = Manifest.model_validate_json(self.path.read_text(encoding="utf-8"))
        self.root = (self.path.parent / self.manifest.root).resolve()
        self.by_id = {asset.id: asset for asset in self.manifest.assets}

    def resolve(self, asset_id: str) -> Path:
        if asset_id not in self.by_id:
            raise QualityError(f"Unknown asset ID: {asset_id}")
        relative = self.by_id[asset_id].path.replace("\\", "/")
        posix = PurePosixPath(relative)
        win = PureWindowsPath(relative)
        if posix.is_absolute() or win.drive or ".." in posix.parts:
            raise QualityError(f"Asset path must be relative to the catalog root: {asset_id}")
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise QualityError(f"Asset symlink escapes the catalog root: {asset_id}")
        if not target.is_file():
            raise QualityError(f"Missing asset file: {asset_id}")
        if target.suffix.lower() not in {".gif", ".png", ".jpg", ".jpeg", ".webp"}:
            raise QualityError(f"Unsupported asset format: {asset_id}")
        return target

    def validate(self, labels: list[str] | None = None) -> None:
        for label in labels or []:
            matching = [x for x in self.by_id.values() if x.kind == "character" and x.label == label]
            if not matching:
                raise QualityError(f"No character asset for label {label}")
        if not any(x.kind == "background" for x in self.by_id.values()):
            raise QualityError("No backgrounds in catalog")
        for key in self.by_id:
            self.resolve(key)

    def context(self, labels: list[str] | None = None) -> list[dict]:
        # Filesystem paths are deliberately never sent to the LLM.
        return [x.model_dump(exclude={"path", "provenance"}) for x in self.by_id.values()
                if labels is None or x.kind == "background" or x.label in labels]
