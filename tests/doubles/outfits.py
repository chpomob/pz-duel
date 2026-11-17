"""Le registre des tenues des tests (P9F25) : un extrait de `clothing.xml`, jamais le jeu installé.

`tests/fixtures/clothing.xml` porte les tenues de la sélection dans les mêmes listes que PZ 42 ;
`StripperWhite` n'y est pas, comme dans le jeu. Un module de tests qui choisit un uniforme reprend
`outfit_registry` : `from doubles.outfits import outfit_registry  # noqa: F401`.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from service import settings as world_settings

CLOTHING_REGISTRY = Path(__file__).resolve().parent.parent / "fixtures" / "clothing.xml"


@pytest.fixture(autouse=True)
def outfit_registry():
    """Pose le registre du dépôt pour le test, puis rend l'offre qu'avait la machine."""
    field = world_settings.FIELD_BY_KEY["zombie_outfit"]
    before = (
        copy.deepcopy(field),
        dict(world_settings.OUTFIT_SEX),
        dict(world_settings.OUTFIT_REGISTRY),
    )
    report = world_settings.use_outfit_registry(CLOTHING_REGISTRY)
    assert report["error"] is None and report["withdrawn"] == [], report
    try:
        yield CLOTHING_REGISTRY
    finally:
        field.clear()
        field.update(before[0])
        world_settings.OUTFIT_SEX.clear()
        world_settings.OUTFIT_SEX.update(before[1])
        world_settings.OUTFIT_REGISTRY.clear()
        world_settings.OUTFIT_REGISTRY.update(before[2])
