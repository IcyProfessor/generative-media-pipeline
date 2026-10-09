"""Explicit registry: config files cannot import arbitrary Python code."""
from collections.abc import Callable
from .base import Recipe
from ..models import RecipeSpec
from ..errors import PipelineError

_FACTORIES: dict[str, Callable[[dict], Recipe]] = {}


def register_recipe(name: str, factory: Callable[[dict], Recipe]) -> None:
    if name in _FACTORIES or name in {"alphabet", "counting"}:
        raise ValueError(f"Recipe is already registered: {name}")
    _FACTORIES[name] = factory


def get_recipe(spec: RecipeSpec) -> Recipe:
    from .alphabet import AlphabetRecipe
    from .counting import CountingRecipe
    factories = {"alphabet": AlphabetRecipe, "counting": CountingRecipe, **_FACTORIES}
    if spec.name not in factories:
        raise PipelineError(f"Unknown recipe: {spec.name}")
    recipe = factories[spec.name](spec.options)
    if recipe.name != spec.name:
        raise PipelineError("Recipe registry/name mismatch")
    return recipe
