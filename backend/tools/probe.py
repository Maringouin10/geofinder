"""Diagnostic d'une source d'images : montre les requêtes et les réponses brutes.

    python -m backend.tools.probe kartaview --city Paris
    python -m backend.tools.probe mapillary --lat 48.8566 --lng 2.3522
    python -m backend.tools.probe all

Sert à transformer un « HTTP 400 » opaque en message d'erreur exploitable :
chaque point d'accès essayé est affiché avec le début de sa réponse.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from backend.app import providers  # noqa: E402
from backend.app.geocode import City, GeocodeError, geocode_city  # noqa: E402
from backend.app.providers import CollectContext, CollectOptions  # noqa: E402

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def resolve_city(name: str, lat: Optional[float], lng: Optional[float]) -> City:
    if lat is not None and lng is not None:
        d = 0.02
        return City(name, f"{name} (coordonnées fournies)", lat, lng,
                    lat - d, lat + d, lng - d, lng + d)
    try:
        city = geocode_city(name)
        print(f"{DIM}Géocodage : {city.display_name} → {city.lat:.4f}, {city.lng:.4f}{RESET}")
        return city
    except GeocodeError as exc:
        print(f"{RED}Géocodage impossible : {exc}{RESET}")
        print("→ Relance avec --lat et --lng pour contourner Nominatim.")
        raise SystemExit(2)


def probe_one(name: str, city: City, max_panos: int) -> bool:
    provider = providers.get(name)
    status = provider.status()
    print(f"\n=== {provider.label} ({name}) ===")
    if not status.ready:
        print(f"{RED}non configurée{RESET} : {status.reason}")
        return False

    ctx = CollectContext(progress=lambda found, note: print(f"{DIM}  {note}{RESET}"))
    try:
        views = provider.discover(city, CollectOptions(max_panos=max_panos, headings=2), ctx)
    except Exception as exc:  # noqa: BLE001 - c'est justement ce qu'on veut voir
        print(f"{RED}ÉCHEC{RESET}\n{exc}")
        return False

    print(f"{GREEN}OK{RESET} — {len(views)} vues sur {len({v.place_id for v in views})} lieux")
    for view in views[:3]:
        print(f"  {view.lat:.5f},{view.lng:.5f} cap={view.heading:3d}  {view.ref[:90]}")

    if views:
        print(f"{DIM}  test de téléchargement de la première vue…{RESET}")
        try:
            raw = provider.fetch(views[0], provider.session())
            print(f"{GREEN}  image reçue : {len(raw)} octets{RESET}")
        except Exception as exc:  # noqa: BLE001
            print(f"{RED}  téléchargement impossible : {exc}{RESET}")
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnostic des sources d'images GeoFinder")
    parser.add_argument("provider", nargs="?", default="all",
                        help="mapillary | kartaview | google | demo | all")
    parser.add_argument("--city", default="Paris")
    parser.add_argument("--lat", type=float, help="évite le géocodage")
    parser.add_argument("--lng", type=float, help="évite le géocodage")
    parser.add_argument("--max-panos", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="état des sources en JSON")
    args = parser.parse_args()

    if args.json:
        print(json.dumps([s.__dict__ for s in providers.statuses()], indent=2, ensure_ascii=False))
        return 0

    print("Sources déclarées :")
    for s in providers.statuses():
        mark = f"{GREEN}prête{RESET}" if s.ready else f"{RED}non configurée{RESET}"
        print(f"  {s.name:11} {mark} {DIM}{s.reason}{RESET}")
    print(f"\n« auto » choisirait : {providers.resolve_auto()}")

    city = resolve_city(args.city, args.lat, args.lng)
    names = providers.names() if args.provider == "all" else [args.provider]

    ok = False
    for name in names:
        if name == "demo" and args.provider == "all":
            continue          # inutile de tester la source synthétique
        ok |= probe_one(name, city, args.max_panos)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
