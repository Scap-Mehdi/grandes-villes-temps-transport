#!/usr/bin/env python3
"""Price per m² of each map cell, from the property sales of DVF (Etalab, geolocated).

Usage: python3 build_prices.py <city>
  reads  data/<city>/dvf_<year>_<department>.csv.gz (fetch_data.py <city> --prices-only) and site/data/<city>.json
  writes site/data/<city>.prices.json and sources/<city>.prices.json (provenance)

Each cell gets the median price of the nearest sales (apartments, last years published). The number of sales behind
the value is kept: the map shows how reliable a cell is. A cell without enough sales nearby falls back to the median
of its borough (arrondissement, commune), flagged with 0 sales. Prices are per m² of built area (`surface_reelle_bati`).
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from cities import load_city

ROOT = Path(__file__).resolve().parent

BUCKET_METERS = 400.0
RADII_METERS = (600.0, 1000.0, 1500.0)  # tried in turn until MIN_SALES sales are found
NEAREST_SALES = 40  # the median is taken over this many nearest sales
MIN_SALES = 15
MIN_PRICE, MAX_PRICE = 1000.0, 30000.0  # €/m² outside of this range: input errors, atypical sales
MIN_SURFACE, MAX_SURFACE = 9.0, 300.0  # m²
LOCAL_TYPES_EXCLUDED = {"Maison", "Local industriel. commercial ou assimilé"}

Sale = Tuple[float, float, float]  # x, y, €/m²


def number(value: str) -> Optional[float]:
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def sales_of_mutations(rows: Iterable[dict], kind: str = "Appartement") -> List[Tuple[str, float, float, float, float, int]]:
    """One sale per mutation that sells exactly one `kind` of local and nothing else of value
    (no house, no shop in the same deed: their prices cannot be shared out).
    Returns (date, lon, lat, €/m², surface, rooms)."""
    mutations: Dict[str, dict] = {}
    for row in rows:
        if row["nature_mutation"] != "Vente":
            continue
        mutation = mutations.setdefault(row["id_mutation"], {"locals": {}, "other": False, "dispositions": set(), "row": row})
        mutation["dispositions"].add(row["numero_disposition"])
        local = row["type_local"]
        if local in LOCAL_TYPES_EXCLUDED:
            mutation["other"] = True
        elif local == kind:
            # Same local repeated over several parcels: counted once.
            key = (row["lot1_numero"], row["surface_reelle_bati"], row["nombre_pieces_principales"], row["longitude"], row["latitude"])
            mutation["locals"][key] = row
    sales = []
    for mutation in mutations.values():
        if mutation["other"] or len(mutation["locals"]) != 1 or len(mutation["dispositions"]) != 1:
            continue
        row = next(iter(mutation["locals"].values()))
        value, surface = number(row["valeur_fonciere"]), number(row["surface_reelle_bati"])
        lon, lat = number(row["longitude"]), number(row["latitude"])
        if None in (value, surface, lon, lat) or not MIN_SURFACE <= surface <= MAX_SURFACE:
            continue
        price = value / surface
        if MIN_PRICE <= price <= MAX_PRICE:
            sales.append((row["date_mutation"], lon, lat, price, surface, int(number(row["nombre_pieces_principales"]) or 0)))
    return sales


def read_dvf(paths: Sequence[Path], kind: str = "Appartement") -> List[Tuple[str, float, float, float, float, int]]:
    sales = []
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            sales += sales_of_mutations(csv.DictReader(handle), kind)
    return sales


def lonlat_to_xy(lon: float, lat: float, lat0: float) -> Tuple[float, float]:
    """Same projection as build_data.py and the browser (metres, x east, y north)."""
    return lon * 111_320.0 * math.cos(math.radians(lat0)), lat * 111_320.0


class SaleIndex:
    def __init__(self, sales: Sequence[Sale]):
        self.sales = sales
        self.buckets: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for i, (x, y, _) in enumerate(sales):
            self.buckets[(int(x // BUCKET_METERS), int(y // BUCKET_METERS))].append(i)

    def nearest(self, x: float, y: float, radius: float, count: int) -> List[Tuple[float, float]]:
        """(distance, €/m²) of the `count` nearest sales within `radius`."""
        reach = int(radius // BUCKET_METERS) + 1
        bx, by = int(x // BUCKET_METERS), int(y // BUCKET_METERS)
        found = []
        for i in range(bx - reach, bx + reach + 1):
            for j in range(by - reach, by + reach + 1):
                for index in self.buckets.get((i, j), ()):
                    sx, sy, price = self.sales[index]
                    d = math.hypot(sx - x, sy - y)
                    if d <= radius:
                        found.append((d, price))
        found.sort()
        return found[:count]


def point_in_ring(x: float, y: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    for k, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(k + 1) % len(ring)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


def areas_of(data: dict) -> List[dict]:
    """Smallest areas of the map: arrondissements when the city has them, communes otherwise."""
    return [
        {"name": area["name"], "rings": [polygon[0] for polygon in area["polygons"]]}
        for area in (data.get("arrondissements") or data["boroughs"])
    ]


def area_index(areas: Sequence[dict], x: float, y: float) -> int:
    for index, area in enumerate(areas):
        if any(point_in_ring(x, y, ring) for ring in area["rings"]):
            return index
    return -1


def cell_prices(sales: Sequence[Sale], cells: Sequence[Tuple[float, float]], areas: Sequence[dict]) -> Tuple[List[int], List[int]]:
    """€/m² and number of sales behind it, for each cell (x, y). 0 €/m²: no data."""
    index = SaleIndex(sales)
    prices = [0] * len(cells)
    counts = [0] * len(cells)
    fallback = []
    for k, (x, y) in enumerate(cells):
        for radius in RADII_METERS:
            near = index.nearest(x, y, radius, NEAREST_SALES)
            if len(near) >= MIN_SALES:
                prices[k] = round(statistics.median(price for _, price in near))
                counts[k] = len(near)
                break
        else:
            fallback.append(k)
    if fallback:
        by_area: Dict[int, List[float]] = defaultdict(list)
        for x, y, price in sales:
            by_area[area_index(areas, x, y)].append(price)  # -1: outside every borough
        for k in fallback:
            area = area_index(areas, *cells[k])
            members = by_area.get(area) if area >= 0 else None
            if members and len(members) >= MIN_SALES:
                prices[k] = round(statistics.median(members))
    return prices, counts


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    city = load_city(sys.argv[1])
    buy = city.get("prices", {}).get("buy")
    if not buy:
        sys.exit(f"{city['name']} : pas de prix d'achat configuré (clé `prices.buy` de cities/{city['slug']}.json)")
    data_dir = ROOT / "data" / city["slug"]
    paths = sorted(data_dir.glob("dvf_*.csv.gz"))
    if not paths:
        sys.exit(f"Aucun fichier DVF dans {data_dir} : lancez fetch_data.py {city['slug']} --prices-only")
    data = json.loads((ROOT / "site" / "data" / f"{city['slug']}.json").read_text(encoding="utf-8"))

    lat0 = data["meta"]["lat0"]
    sales_raw = read_dvf(paths)
    sales = [(*lonlat_to_xy(lon, lat, lat0), price) for _, lon, lat, price, _, _ in sales_raw]
    cells = [tuple(cell["point"]) for cell in data["cells"]]
    prices, counts = cell_prices(sales, cells, areas_of(data))

    dates = sorted(sale[0] for sale in sales_raw)
    covered = [p for p in prices if p]
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    files = {name: entry for name, entry in manifest.items() if name.startswith("dvf_")}
    meta = {
        "source": "DVF géolocalisées (Etalab, DGFiP)",
        "dataset": "https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees",
        "licence": "Licence Ouverte 2.0",
        "kind": "Appartement",
        "from": dates[0],
        "to": dates[-1],
        "sales": len(sales),
        "median": round(statistics.median(price for _, _, price in sales)),
        "cellsCovered": round(100 * len(covered) / len(prices)),
        "minSales": MIN_SALES,
    }
    out = ROOT / "site" / "data" / f"{city['slug']}.prices.json"
    out.write_text(json.dumps({"meta": meta, "buy": prices, "buyN": counts}, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    provenance = {
        "city": city["slug"],
        "builtAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **meta,
        "files": files,
        "filters": {
            "nature": "Vente",
            "perM2": [MIN_PRICE, MAX_PRICE],
            "surface": [MIN_SURFACE, MAX_SURFACE],
            "nearestSales": NEAREST_SALES,
            "radiiMeters": list(RADII_METERS),
        },
    }
    (ROOT / "sources" / f"{city['slug']}.prices.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {out.relative_to(ROOT)} ({out.stat().st_size / 1e3:.0f} Ko) : {len(sales)} ventes du {meta['from']} au {meta['to']}, "
        f"médiane {meta['median']} €/m², {meta['cellsCovered']} % des cases couvertes "
        f"({sum(1 for c in counts if c >= MIN_SALES)} avec au moins {MIN_SALES} ventes)"
    )


if __name__ == "__main__":
    main()
