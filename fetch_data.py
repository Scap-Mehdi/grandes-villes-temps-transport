#!/usr/bin/env python3
"""Download the raw sources of a city into data/<city>/.

Usage: python3 fetch_data.py <city> [--gtfs-only | --context-only | --rivers-only]
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
import urllib.parse
import urllib.request
from pathlib import Path

from cities import load_city

ROOT = Path(__file__).resolve().parent
USER_AGENT = "tram.camilleroux.com/0.2 (build script)"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


def bbox(values) -> str:
    return ",".join(str(v) for v in values)


def download(url: str, data: bytes | None = None) -> bytes:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def overpass(query: str) -> bytes:
    payload = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(6):
        for url in OVERPASS_URLS:
            try:
                body = download(url, payload)
                json.loads(body)
                return body
            except Exception as error:  # noqa: BLE001 - Overpass is often busy, just retry
                print(f"  {url} failed ({error}), retrying…")
        time.sleep(10 * (attempt + 1))
    raise RuntimeError("Overpass unavailable")


def record(out: Path, name: str, source: str, how: str = "download") -> None:
    """Note in data/<city>/manifest.json where each raw file comes from and when it was fetched."""
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    path = out / name
    fetched = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) if how == "manual" else datetime.now(timezone.utc)
    manifest[name] = {
        "source": source,
        "how": how,
        "fetchedAt": fetched.isoformat(timespec="seconds"),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_context(city: dict, out: Path) -> None:
    """Land around the metropolis, for coastal cities: whatever is left uncovered on the map is drawn as sea."""
    departments = city.get("seaDepartments", [])
    if departments:
        print(f"Communes voisines (départements {', '.join(departments)})…")
        features = []
        urls = []
        for code in departments:
            url = f"https://geo.api.gouv.fr/departements/{code}/communes?fields=nom,code&format=geojson&geometry=contour"
            features += json.loads(download(url))["features"]
            urls.append(url)
        (out / "context.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
        record(out, "context.geojson", " + ".join(urls))
    relations = city.get("contextOsmRelations", [])
    if relations:
        print("Territoires voisins hors de France (OSM)…")
        query = "[out:json][timeout:110];(" + "".join(f"relation({rel});" for rel in relations) + ");out geom;"
        (out / "context_osm.json").write_bytes(overpass(query))
        record(out, "context_osm.json", f"Overpass API: {query}")


def fetch_rivers(city: dict, out: Path) -> None:
    """Rivers crossed on foot only by a bridge (`"rivers"`: names, and their « La Loire - Bras de Pirmil » parts)."""
    if not city.get("rivers"):
        return
    print("Cours d'eau (OSM)…")
    names = "|".join(re.escape(name) for name in city["rivers"])
    query = (
        f'[out:json][timeout:110];way["waterway"="river"]["name"~"^({names})( - .*)?$"]["tunnel"!~"."]'
        f'({bbox(city["osmBbox"])});out geom;'
    )
    (out / "osm_rivers.json").write_bytes(overpass(query))
    record(out, "osm_rivers.json", f"Overpass API: {query}")
    # Bridges open to pedestrians: the ones over these rivers are kept by build_data.py.
    query = (
        '[out:json][timeout:110];way["bridge"]["highway"]'
        '["highway"!~"^(motorway|motorway_link|trunk|trunk_link|construction|proposed)$"]["foot"!="no"]'
        f'({bbox(city["osmBbox"])});out geom;'
    )
    (out / "osm_bridges.json").write_bytes(overpass(query))
    record(out, "osm_bridges.json", f"Overpass API: {query}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    city = load_city(sys.argv[1])
    out = ROOT / "data" / city["slug"]
    out.mkdir(parents=True, exist_ok=True)
    if "--context-only" in sys.argv:
        fetch_context(city, out)
        return
    if "--rivers-only" in sys.argv:
        fetch_rivers(city, out)
        return

    print(f"GTFS {city['network']}…")
    if city.get("gtfsManual"):
        # Some operators (TCL on data.grandlyon.com) require an account: the file is downloaded by hand.
        if not (out / "gtfs.zip").exists():
            sys.exit(f"Téléchargez le GTFS à la main ({city['gtfsManual']}) et posez-le dans {out / 'gtfs.zip'}")
        print(f"  fichier manuel conservé ({city['gtfsManual']})")
        record(out, "gtfs.zip", city["gtfsUrl"], how="manual")
    else:
        (out / "gtfs.zip").write_bytes(download(city["gtfsUrl"]))
        record(out, "gtfs.zip", city["gtfsUrl"])
    if "--gtfs-only" in sys.argv:
        return

    print(f"Communes de {city['metropole']}…")
    communes_url = f"https://geo.api.gouv.fr/epcis/{city['epci']}/communes?fields=nom,code&format=geojson&geometry=contour"
    (out / "communes.geojson").write_bytes(download(communes_url))
    record(out, "communes.geojson", communes_url)
    if city.get("arrondissements"):
        print("Arrondissements municipaux…")
        url = (f"https://geo.api.gouv.fr/communes?type=arrondissement-municipal&codeParent={city['arrondissements']}"
               "&fields=nom,code&format=geojson&geometry=contour")
        (out / "arrondissements.geojson").write_bytes(download(url))
        record(out, "arrondissements.geojson", url)

    if city.get("railGeometry") == "osm":
        print("Tracés des lignes (OSM)…")
        query = f'[out:json][timeout:110];relation["route"~"^(tram|subway|light_rail|funicular)$"]({bbox(city["osmRailBbox"])});out geom;'
        (out / "osm_rail.json").write_bytes(overpass(query))
        record(out, "osm_rail.json", f"Overpass API: {query}")

    print("Eau et parcs (OSM)…")
    area, parks = bbox(city["osmBbox"]), bbox(city["parksBbox"])
    query = (
        "[out:json][timeout:180];("
        f'relation["natural"="water"]({area});'
        f'way["natural"="water"]({area});'
        f'relation["leisure"="park"]({parks});'
        f'way["leisure"="park"]({parks});'
        ");out geom;"
    )
    (out / "osm_water_parks.json").write_bytes(overpass(query))
    record(out, "osm_water_parks.json", f"Overpass API: {query}")
    fetch_rivers(city, out)
    fetch_context(city, out)


if __name__ == "__main__":
    main()
