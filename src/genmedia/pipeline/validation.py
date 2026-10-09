"""Structural checks only. Semantic rules live in recipes."""
from ..assets.manifest import Catalog
from ..models import ScenePlan, ImageLayer
from ..errors import QualityError


def validate_plan(plan: ScenePlan, catalog: Catalog) -> None:
    if abs(plan.scenes[0].start) > 1e-6:
        raise QualityError("Scene plan must start at zero")
    if abs(plan.scenes[-1].end - plan.duration) > 1e-6:
        raise QualityError("Scene plan must cover audio to its end")
    ids = [x.id for x in plan.scenes]
    if len(ids) != len(set(ids)):
        raise QualityError("Duplicate scene IDs")
    for first, second in zip(plan.scenes, plan.scenes[1:]):
        if abs(first.end - second.start) > 1e-6:
            raise QualityError("Scene plan has gaps or overlaps")
    for scene in plan.scenes:
        background = catalog.by_id.get(scene.background_id)
        if background is None or background.kind != "background":
            raise QualityError("Scene has invalid background")
        catalog.resolve(background.id)
        for layer in scene.layers:
            if isinstance(layer, ImageLayer):
                asset = catalog.by_id.get(layer.asset_id)
                if asset is None or asset.kind != "character":
                    raise QualityError("Scene has invalid image asset")
                catalog.resolve(asset.id)
