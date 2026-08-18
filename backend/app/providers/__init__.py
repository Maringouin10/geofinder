"""Registre des sources d'imagerie de rue."""
from __future__ import annotations

from typing import Dict, List

from .base import (
    Cancelled,
    CollectContext,
    CollectOptions,
    Provider,
    ProviderError,
    ProviderStatus,
    ViewRef,
)
from .demo import DemoProvider
from .google import GoogleProvider
from .kartaview import KartaViewProvider
from .mapillary import MapillaryProvider

_REGISTRY: Dict[str, Provider] = {
    p.name: p
    for p in (MapillaryProvider(), KartaViewProvider(), GoogleProvider(), DemoProvider())
}

# Ordre de préférence quand le provider demandé est « auto ».
# Mapillary d'abord (meilleure couverture), puis KartaView qui ne demande aucune
# clé, puis Google, et la démo en dernier recours pour que l'app reste utilisable.
AUTO_ORDER = ("mapillary", "kartaview", "google", "demo")


def get(name: str) -> Provider:
    if name in (None, "", "auto"):
        return _REGISTRY[resolve_auto()]
    provider = _REGISTRY.get(name)
    if provider is None:
        raise ProviderError(f"Source d'images inconnue : « {name} »")
    return provider


def resolve_auto() -> str:
    """Première source du classement qui soit réellement utilisable."""
    for name in AUTO_ORDER:
        if _REGISTRY[name].status().ready:
            return name
    return "demo"


def statuses() -> List[ProviderStatus]:
    return [_REGISTRY[name].status() for name in AUTO_ORDER]


def names() -> List[str]:
    return list(AUTO_ORDER)


__all__ = [
    "Cancelled",
    "CollectContext",
    "CollectOptions",
    "Provider",
    "ProviderError",
    "ProviderStatus",
    "ViewRef",
    "get",
    "names",
    "resolve_auto",
    "statuses",
]
