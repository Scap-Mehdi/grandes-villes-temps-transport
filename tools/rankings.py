#!/usr/bin/env python3
"""Rankings read straight from the official timetables (GTFS), tram and metro only, no travel-time model.

Published on the site: most served station, peak frequency, longest line, trips per day, last Saturday-night
passage at the centre. Night trips are encoded differently by each operator (25:30 on the day before or 01:30 on the
day after): times are read over a transport day from 3:30 to 3:30, across the neighbouring service days.

Usage: python3 tools/rankings.py [city …]   (writes sources/rankings.json and prints a summary)

For each city, on the reference weekday chosen by build_data.py (a plain school-term Tuesday or Thursday)
and on the following Saturday:
- first and last passage at the centre of the map (weekday), last passage there on Saturday night;
- the most served station (tram/metro passages on the weekday);
- per line, passages between 8 and 9 am at its busiest stop, in the busiest direction;
- per line, scheduled time from one end to the other on its most common trip pattern;
- number of tram/metro trips on the weekday.
"""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import build_data as bd  # noqa: E402
from cities import load_cities  # noqa: E402

RANKED_MODES = {"tram", "metro"}
PEAK = (8 * 3600, 9 * 3600)
# A transport day runs from 3 am to 3 am: night trips count for the evening they belong to, whatever the encoding
# (25:30 on the day before or 01:30 on the day after).
DAY_START = 3 * 3600 + 1800  # 3:30: Nantes' last Saturday tram reaches the centre at 3:00, no centre opens before 4


def hhmm(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours % 24:02d}:{minutes:02d}" + (" (+1)" if hours >= 24 else "")


def read_boardings(archive: zipfile.ZipFile, trips: dict) -> dict:
    """stop_times of the given trips, without the stops where passengers cannot board (pickup_type 1)."""
    stop_times = defaultdict(list)
    with archive.open("stop_times.txt") as handle:
        reader = csv.reader(io.TextIOWrapper(handle, encoding="utf-8-sig"))
        header = next(reader)
        col = {name: header.index(name) for name in ("trip_id", "stop_sequence", "stop_id", "arrival_time", "departure_time")}
        pickup = header.index("pickup_type") if "pickup_type" in header else None
        for row in reader:
            if row[col["trip_id"]] not in trips or not row[col["arrival_time"]]:
                continue
            boarding = pickup is None or row[pickup].strip() != "1"
            stop_times[row[col["trip_id"]]].append(
                (int(row[col["stop_sequence"]]), row[col["stop_id"]], bd.parse_time(row[col["arrival_time"]]),
                 bd.parse_time(row[col["departure_time"]]), boarding)
            )
    return stop_times


def fetched_at(data_dir: Path) -> str:
    """Download date of the timetable: from fetch_data.py's manifest, else the file date."""
    manifest = data_dir / "manifest.json"
    if manifest.exists():
        gtfs = json.loads(manifest.read_text(encoding="utf-8")).get("gtfs.zip", {})
        if gtfs.get("fetchedAt"):
            return gtfs["fetchedAt"][:10]
    return date.fromtimestamp((data_dir / "gtfs.zip").stat().st_mtime).isoformat()


def city_rankings(city: dict) -> dict:
    bd.LAT0 = city["lat0"]
    data_dir = ROOT / "data" / city["slug"]
    with zipfile.ZipFile(data_dir / "gtfs.zip") as archive:
        routes = {row["route_id"]: row for row in bd.read_gtfs_table(archive, "routes.txt")}
        overrides = city.get("routeModes", {})
        modes = {
            route_id: overrides.get(route_id) or overrides.get(row.get("route_short_name", ""), bd.route_mode(row.get("route_type", "3")))
            for route_id, row in routes.items()
            if not bd.route_excluded(row, city)
        }
        ranked = {route_id for route_id, mode in modes.items() if mode in RANKED_MODES}
        services = bd.services_by_date(
            list(bd.read_gtfs_table(archive, "calendar.txt")), list(bd.read_gtfs_table(archive, "calendar_dates.txt"))
        )
        all_trips = [row for row in bd.read_gtfs_table(archive, "trips.txt") if row["route_id"] in modes and not (row.get("TAD") or "").strip()]
        weekday = bd.pick_reference_date(services, Counter(row["service_id"] for row in all_trips))
        saturday = weekday + timedelta(days=(5 - weekday.weekday()) % 7)
        while saturday in services and not services[saturday] and saturday < max(services):
            saturday += timedelta(days=7)
        def trips_on(day):
            return {row["trip_id"]: row for row in all_trips if row["route_id"] in ranked and row["service_id"] in services.get(day, ())}

        day_trips = {"weekday": trips_on(weekday), "saturday": trips_on(saturday)}
        # Neighbouring service days, for the trips of the transport day encoded on them.
        around = {offset: {day: trips_on(day + timedelta(days=offset)) for day in (weekday, saturday)} for offset in (-1, 1)}
        wanted = {**day_trips["weekday"], **day_trips["saturday"]}
        for by_day in around.values():
            for trips in by_day.values():
                wanted.update(trips)
        stop_times = read_boardings(archive, wanted)
        stops = {row["stop_id"]: row for row in bd.read_gtfs_table(archive, "stops.txt")}

    def line(route_id: str) -> str:
        return routes[route_id].get("route_short_name") or route_id

    def station(stop_id: str) -> str:
        return bd.display_name(stops[stop_id]["stop_name"])

    result = {
        "city": city["name"],
        "slug": city["slug"],
        "path": city["path"],
        "externalUrl": city.get("externalUrl"),
        "network": city["network"],
        "source": {
            "dataset": city.get("gtfsDataset") or city.get("gtfsUrl"),
            "licence": city["gtfsLicence"],
            "fetchedAt": fetched_at(data_dir),
        },
        "weekday": weekday.isoformat(),
        "saturday": saturday.isoformat(),
    }

    # Departures (a passenger can board): every stop of a trip but the last one, where boarding is allowed.
    def departures(trips: dict):
        for trip_id, trip in trips.items():
            sequence = sorted(stop_times.get(trip_id, []))
            for _, stop_id, _, dep, boarding in sequence[:-1]:
                if boarding:
                    yield trip, stop_id, dep

    week = list(departures(day_trips["weekday"]))

    def transport_day(day, label: str) -> list:
        """Departures between 3 am on `day` and 3 am the next day, in seconds since midnight of `day`."""
        result = []
        for offset, trips in ((0, day_trips[label]), (-1, around[-1][day]), (1, around[1][day])):
            for trip, stop_id, dep in departures(trips):
                when = dep + offset * 86400
                if DAY_START <= when < DAY_START + 86400:
                    result.append((trip, stop_id, when))
        return result

    day = transport_day(weekday, "weekday")
    sat = transport_day(saturday, "saturday")
    # First and last passages at the centre: the station nearest to the map's default start (Comédie, Châtelet…).
    # The very first or last trip of a network leaves a terminus at an hour nobody rides it; the centre is what
    # riders know and what the station timetable shows.
    lat, lon = city["defaultFrom"]["lat"], city["defaultFrom"]["lon"]
    scale = math.cos(math.radians(lat))
    centre_stop = min(
        {stop_id for _, stop_id, _ in day},
        key=lambda stop_id: math.hypot(float(stops[stop_id]["stop_lat"]) - lat, (float(stops[stop_id]["stop_lon"]) - lon) * scale),
    )
    centre = bd.normalize_name(stops[centre_stop]["stop_name"])

    def at_centre(items: list, pick) -> dict | None:
        items = [item for item in items if bd.normalize_name(stops[item[1]]["stop_name"]) == centre]
        if not items:
            return None
        trip, _, when = pick(items, key=lambda item: item[2])
        return {"time": hhmm(when), "seconds": when, "line": line(trip["route_id"])}

    result["centre"] = {
        "station": station(centre_stop),
        "firstWeekday": at_centre(day, min),
        "lastWeekday": at_centre(day, max),
        "lastSaturday": at_centre(sat, max),
    }

    # Most served station: passages of every tram/metro line, stops grouped by name.
    passages = Counter(bd.normalize_name(stops[stop_id]["stop_name"]) for _, stop_id, _ in week)
    names = {bd.normalize_name(stops[stop_id]["stop_name"]): station(stop_id) for _, stop_id, _ in week}
    ranking = passages.most_common()
    top_count = ranking[0][1]
    tied = [names[key] for key, count in ranking if count == top_count]
    # Several stations with the same count (a single line): no champion to name.
    result["busiestStation"] = {"station": tied[0] if len(tied) == 1 else None, "tied": tied if len(tied) > 1 else [], "passages": top_count}

    # Peak frequency and end-to-end time, per line.
    lines = {}
    for route_id in sorted({trip["route_id"] for trip in day_trips["weekday"].values()}, key=line):
        trips = {trip_id: trip for trip_id, trip in day_trips["weekday"].items() if trip["route_id"] == route_id}
        peak = Counter(
            (stop_id, trip.get("direction_id") or "0") for trip, stop_id, dep in departures(trips) if PEAK[0] <= dep < PEAK[1]
        )
        patterns = defaultdict(list)
        for trip_id in trips:
            sequence = sorted(stop_times.get(trip_id, []))
            if len(sequence) >= 2:
                patterns[tuple(stop[1] for stop in sequence)].append(sequence[-1][2] - sequence[0][3])
        # The whole line, not its most frequent variant: the longest of the main stop sequences (both directions), those
        # run at least a third as often as the most frequent one. Short turns and occasional runs stay out (T3 Montpellier:
        # Mosson → Pérols has one more run than Pérols → Juvignac, 6 min longer).
        most_runs = max(len(durations) for durations in patterns.values())
        pattern, durations = max(
            ((pattern, durations) for pattern, durations in patterns.items() if len(durations) * 3 >= most_runs),
            key=lambda item: statistics.median(item[1]),
        )
        busiest_peak = max(peak.values()) if peak else 0
        lines[line(route_id)] = {
            "mode": modes[route_id],
            "color": f"#{(routes[route_id].get('route_color') or '888888').strip().lstrip('#') or '888888'}",
            "peakPassages": busiest_peak,
            "peakHeadway": round(60 / busiest_peak, 1) if busiest_peak else None,
            "endToEndMinutes": round(statistics.median(durations) / 60),
            "endToEnd": f"{station(pattern[0])} → {station(pattern[-1])}",
        }
    result["lines"] = lines
    result["weekdayTrips"] = len(day_trips["weekday"])
    return result


def main() -> None:
    slugs = sys.argv[1:]
    # Rankings of France's trams and metros: cities abroad (Montréal) have a map, but are not ranked.
    cities = [
        city for city in load_cities(include_rankings_only=True)
        if city["country"] == "FR" and (not slugs or city["slug"] in slugs)
    ]
    out = ROOT / "sources" / "rankings.json"
    previous = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    results = {}
    for city in cities:
        if not (ROOT / "data" / city["slug"] / "gtfs.zip").exists():
            # Sources stay local: without them, keep the figures computed last time.
            if city["slug"] in previous:
                results[city["slug"]] = previous[city["slug"]]
                print(f"{city['name']:16s} pas de GTFS local, chiffres précédents conservés")
            continue
        results[city["slug"]] = city_rankings(city)
        r = results[city["slug"]]
        print(
            f"{city['name']:16s} {r['weekday']} | centre {r['centre']['station'][:18]:18s} 1er {r['centre']['firstWeekday']['time']} "
            f"dernier {r['centre']['lastWeekday']['time']} samedi {r['centre']['lastSaturday']['time']} | station {r['busiestStation']['station'] or 'égalité'} "
            f"{r['busiestStation']['passages']} | trajets {r['weekdayTrips']}"
        )
    if not slugs:
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
