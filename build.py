#!/usr/bin/env python3
"""Build the whole site: data of each city, pages, preview images, then a control table.

Usage: python3 build.py [city …] [--fetch] [--no-og]
  (no city: all of them; --fetch: download the sources first; --no-og: keep the preview images)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cities import load_cities, load_city

ROOT = Path(__file__).resolve().parent


def run(*args: str) -> None:
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def control_row(slug: str) -> str:
    """One line per city to spot anomalies at a glance (reference day, size, reach, farthest station)."""
    sources = json.loads((ROOT / "sources" / f"{slug}.json").read_text(encoding="utf-8"))
    stats = sources["stats"]
    size = (ROOT / "site" / "data" / f"{slug}.json").stat().st_size / 1e6
    lines = " ".join(f"{line['name']}:{line['headway']:g}" for line in stats["lines"])
    return (
        f"{slug:16s} {sources['referenceDate']} | {size:4.1f} Mo | {stats['railStations']:3d} stations | "
        f"30 min : {stats['within30']:3d} % | plus loin : {stats['farthestStation'][:24]} {stats['farthestMinutes']} min | {lines}"
    )


def main() -> None:
    flags = {arg for arg in sys.argv[1:] if arg.startswith("--")}
    slugs = [arg for arg in sys.argv[1:] if not arg.startswith("--")] or [city["slug"] for city in load_cities()]
    for slug in slugs:
        if "--fetch" in flags:
            run("fetch_data.py", slug)
        run("build_data.py", slug)
        if load_city(slug).get("prices", {}).get("buy") and list((ROOT / "data" / slug).glob("dvf_*.csv.gz")):
            run("build_prices.py", slug)
    run("tools/rankings.py")
    run("build_pages.py")
    if "--no-og" not in flags:
        for slug in [*slugs, "home", "classements"]:
            run("tools/render_og.py", slug)
        run("build_pages.py")  # pages reference the fingerprint of the new images
    print()
    for slug in slugs:
        print(control_row(slug))


if __name__ == "__main__":
    main()
