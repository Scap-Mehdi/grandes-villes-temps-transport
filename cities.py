"""City configurations (cities/<slug>.json), completed with defaults shared by every script.

A config only needs what cannot be deduced: name, network, sources (GTFS, EPCI), centre of the map and the
`kind` of rail network ("tram", "metro" or "metro+tram"). Everything else (titles, labels, OSM areas…) has a
default below and can be overridden in the JSON when a city needs it.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CITIES_DIR = ROOT / "cities"

RAIL_NOUN = {"tram": "tram", "metro": "métro", "metro+tram": "métro et tram"}
RAIL_LABEL = {"tram": "Tram", "metro": "Métro", "metro+tram": "Métro et tram"}
RAIL_STATIONS = {"tram": "stations de tram", "metro": "stations de métro", "metro+tram": "stations de métro et de tram"}
# Half-sizes (degrees of latitude, longitude) of the OSM areas around the centre.
OSM_HALF_SIZE = (0.22, 0.32)
PARKS_HALF_SIZE = (0.06, 0.08)


def lowercase_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text.startswith(("Place", "Hôtel")) else text


def with_defaults(raw: dict) -> dict:
    city = dict(raw)
    kind = city.setdefault("kind", "tram")
    lat, lon = city["defaultFrom"]["lat"], city["defaultFrom"]["lon"]
    city.setdefault("path", f"{city['slug']}/")
    city.setdefault("railNoun", RAIL_NOUN[kind])
    city.setdefault("railLabel", RAIL_LABEL[kind])
    city.setdefault("railStations", RAIL_STATIONS[kind])
    city.setdefault("title", f"{city['name']} à portée de {'tram' if kind == 'tram' else 'métro'}")
    city.setdefault("titleSuffix", f"Temps de trajet en {city['railNoun']} {city['network']}")
    city.setdefault("busNoun", "bus")
    city.setdefault("busLabel", "Bus")
    city.setdefault("area", "de la Métropole" if "métropole" in city["metropole"].lower() else "de l'agglomération")
    city.setdefault("railGeometry", "gtfs")
    # Outside France (`country`), no Base Adresse Nationale: addresses are looked up in OSM through Photon.
    city.setdefault("country", "FR")
    city.setdefault("geocoder", "ban" if city["country"] == "FR" else "photon")
    city.setdefault("lat0", round(lat, 2))
    city.setdefault("osmBbox", [round(lat - OSM_HALF_SIZE[0], 2), round(lon - OSM_HALF_SIZE[1], 2),
                                round(lat + OSM_HALF_SIZE[0], 2), round(lon + OSM_HALF_SIZE[1], 2)])
    city.setdefault("parksBbox", [round(lat - PARKS_HALF_SIZE[0], 2), round(lon - PARKS_HALF_SIZE[1], 2),
                                  round(lat + PARKS_HALF_SIZE[0], 2), round(lon + PARKS_HALF_SIZE[1], 2)])
    city.setdefault("osmRailBbox", city["osmBbox"])
    city.setdefault(
        "ogAlt",
        f"Carte de {city['name']} colorée selon le temps de trajet en {city['railNoun']} depuis "
        f"{lowercase_first(city['defaultFrom']['label'])}, avec les isochrones 15 et 30 minutes.",
    )
    return city


def load_city(slug: str) -> dict:
    return with_defaults(json.loads((CITIES_DIR / f"{slug}.json").read_text(encoding="utf-8")))


def load_cities(include_rankings_only: bool = False) -> list[dict]:
    """Cities with a map. Some cities may only appear in the rankings (`rankingsOnly`, with an `externalUrl` to their map)."""
    cities = [with_defaults(json.loads(path.read_text(encoding="utf-8"))) for path in CITIES_DIR.glob("*.json")]
    cities = [city for city in cities if include_rankings_only or not city.get("rankingsOnly")]
    return sorted(cities, key=lambda city: city["order"])
