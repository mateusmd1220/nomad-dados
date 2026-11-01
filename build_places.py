"""Builds the offline place search files for the NOMAD Maps tab (the app searches all of them).

  lugares-<região>.sqlite  OpenStreetMap of one region (Geofabrik .osm.pbf): POIs, streets,
                           cities and neighbourhoods
  lugares-mundo.sqlite     GeoNames cities1000 (cities of the whole world, Portuguese names
                           included) + OurAirports (airports and heliports of the whole world)

Usage (scripts/baixar_regiao.ps1 runs this):
  pip install osmium
  python scripts/build_places.py --osm <region.osm.pbf> --region <id> --out lugares-<id>.sqlite
  python scripts/build_places.py --world <dir with cities1000.zip, admin1CodesASCII.txt,
                                 countryInfo.txt, airports.csv> --out lugares-mundo.sqlite

The categories must match CATEGORIES in app/src/lib/places.ts.
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sqlite3
import sys
import time
import zipfile

import osmium

SCHEMA_VERSION = 2  # 2: one file per region + lugares-mundo

# ---- OSM tag -> category -------------------------------------------------------------------

URGENT = re.compile(r"\b(upa|pronto[ -]?(socorro|atendimento)|emerg[eê]ncia|samu)\b", re.I)
CIVIL_DEFENSE = re.compile(r"defesa civil", re.I)

CATEGORY_RANK = {
    "hospital": 60,
    "urgencia": 58,
    "clinica": 40,
    "farmacia": 42,
    "policia": 50,
    "bombeiros": 52,
    "defesa_civil": 52,
    "combustivel": 38,
    "agua": 36,
    "abrigo": 40,
    "mercado": 36,
    "escola": 34,
    "ginasio": 34,
    "governo": 40,
    "porto": 45,
    "rodoviaria": 45,
    "militar": 40,
    "ferragens": 32,
    "embaixada": 45,
    "local": 20,
}


def category(tags) -> str | None:
    """Emergency-relevant category for a POI, 'local' for any other named place, None to skip."""
    g = tags.get
    name = g("name", "")
    amenity = g("amenity")
    healthcare = g("healthcare")
    if amenity == "hospital" or healthcare == "hospital":
        return "urgencia" if URGENT.search(name) else "hospital"
    if amenity in ("clinic", "doctors") or healthcare in ("clinic", "centre", "doctor", "urgent_care"):
        return "urgencia" if URGENT.search(name) or healthcare == "urgent_care" else "clinica"
    if amenity == "pharmacy" or healthcare == "pharmacy":
        return "farmacia"
    if amenity == "police":
        return "policia"
    if amenity == "fire_station":
        return "bombeiros"
    if CIVIL_DEFENSE.search(name):
        return "defesa_civil"
    if amenity == "fuel":
        return "combustivel"
    if amenity in ("drinking_water", "water_point") or g("man_made") == "water_tap" or g("natural") == "spring":
        return "agua"
    if (amenity == "shelter" and g("shelter_type") not in ("public_transport", "picnic_shelter")) or g(
        "emergency"
    ) == "assembly_point" or g("social_facility") == "shelter":
        return "abrigo"
    if g("shop") in ("supermarket", "wholesale"):
        return "mercado"
    if amenity in ("school", "university", "college"):
        return "escola"
    if g("leisure") in ("sports_centre", "stadium"):
        return "ginasio"
    if amenity in ("townhall", "courthouse") or g("office") == "government":
        return "governo"
    if g("harbour") == "yes" or amenity == "ferry_terminal" or g("landuse") == "port" or g("industrial") == "port":
        return "porto"
    if amenity == "bus_station":
        return "rodoviaria"
    if g("military") in ("barracks", "base", "office") or g("landuse") == "military":
        return "militar"
    if g("shop") in ("hardware", "doityourself"):
        return "ferragens"
    if g("diplomatic") == "embassy" or amenity == "embassy":
        return "embaixada"
    if not name:
        return None
    if amenity or g("shop") or g("tourism") or g("office") or g("leisure") or g("craft") or g("healthcare"):
        return "local"
    return None


PLACE_RANK = {
    "city": 80,
    "town": 60,
    "village": 42,
    "suburb": 36,
    "neighbourhood": 30,
    "quarter": 30,
    "hamlet": 25,
    "isolated_dwelling": 12,
    "locality": 15,
    "farm": 12,
}
NEAR_CITY = {"city", "town", "village"}
NEAR_DISTRICT = {"suburb", "neighbourhood", "quarter"}

STREET_RANK = {
    "motorway": 30,
    "trunk": 28,
    "primary": 24,
    "secondary": 20,
    "tertiary": 16,
    "unclassified": 10,
    "residential": 10,
    "living_street": 10,
    "pedestrian": 10,
    "road": 8,
    "service": 4,
    "track": 4,
}

KEYS = [
    "amenity", "shop", "healthcare", "tourism", "office", "leisure", "craft", "emergency", "man_made",
    "natural", "harbour", "landuse", "industrial", "military", "diplomatic", "social_facility", "place",
    "highway",
]


def pop_rank(base: int, population: str | int | None) -> int:
    try:
        p = int(str(population).replace(".", "").replace(" ", ""))
    except (TypeError, ValueError):
        return base
    return max(base, min(100, int(20 + 10 * math.log10(max(p, 1)))))


def info(tags) -> dict:
    out = {}
    g = tags.get
    phone = g("phone") or g("contact:phone")
    if phone:
        out["ph"] = phone
    if g("opening_hours"):
        out["oh"] = g("opening_hours")
    street = g("addr:street")
    if street:
        out["ad"] = f"{street}, {g('addr:housenumber')}" if g("addr:housenumber") else street
    if g("emergency") == "yes":
        out["em"] = 1
    if g("operator"):
        out["op"] = g("operator")
    return out


def alt_names(tags) -> str:
    names = {tags.get(k) for k in ("alt_name", "official_name", "short_name", "old_name", "brand", "ref")}
    return " ".join(sorted(n for n in names if n))


class Grid:
    """Nearest named place within a radius, on a coarse lat/lon grid."""

    def __init__(self, cell: float):
        self.cell = cell
        self.cells: dict[tuple[int, int], list[tuple[float, float, str]]] = {}

    def add(self, lat: float, lon: float, name: str):
        self.cells.setdefault((int(lat // self.cell), int(lon // self.cell)), []).append((lat, lon, name))

    def nearest(self, lat: float, lon: float, max_km: float) -> str | None:
        ci, cj = int(lat // self.cell), int(lon // self.cell)
        best, best_d = None, max_km
        k = math.cos(math.radians(lat))
        for i in (ci - 1, ci, ci + 1):
            for j in (cj - 1, cj, cj + 1):
                for plat, plon, name in self.cells.get((i, j), ()):
                    d = 111.2 * math.hypot(plat - lat, (plon - lon) * k)
                    if d < best_d:
                        best, best_d = name, d
        return best


def way_point(way):
    """Middle node of an open way, average of the nodes of a closed one."""
    locs = [n.location for n in way.nodes if n.location.valid()]
    if not locs:
        return None
    if way.is_closed() and len(locs) > 2:
        locs = locs[:-1]
        return sum(l.lat for l in locs) / len(locs), sum(l.lon for l in locs) / len(locs)
    mid = locs[len(locs) // 2]
    return mid.lat, mid.lon


def read_osm(pbf: str, db: sqlite3.Connection, cities: Grid, districts: Grid, index: str = "flex_mem"):
    stage = db.cursor()
    seen_streets: set[tuple[str, int, int]] = set()
    n = 0
    t0 = time.time()
    fp = (
        osmium.FileProcessor(pbf)
        .with_locations(index)
        .with_areas(osmium.filter.KeyFilter("amenity", "healthcare", "military", "landuse", "leisure"))
        .with_filter(osmium.filter.KeyFilter(*KEYS))
    )
    for obj in fp:
        n += 1
        if n % 2_000_000 == 0:
            print(f"  {n:,} objetos OSM ({time.time() - t0:.0f}s)", flush=True)
        tags = obj.tags
        if obj.is_node():
            if not obj.location.valid():
                continue
            lat, lon = obj.location.lat, obj.location.lon
        elif obj.is_way():
            pt = way_point(obj)
            if not pt:
                continue
            lat, lon = pt
        elif obj.is_area():
            if obj.from_way():
                continue  # already handled as a way
            ring = next(iter(obj.outer_rings()), None)
            if ring is None:
                continue
            locs = [nr.location for nr in ring if nr.location.valid()]
            if not locs:
                continue
            lat = sum(l.lat for l in locs) / len(locs)
            lon = sum(l.lon for l in locs) / len(locs)
        else:
            continue

        name = tags.get("name", "")
        place = tags.get("place")
        if place in PLACE_RANK and name and obj.is_node():
            rank = pop_rank(PLACE_RANK[place], tags.get("population"))
            stage.execute(
                "INSERT INTO stage VALUES (?,?,?,?,?,?,?,?)",
                (name, alt_names(tags), "cidade" if place in NEAR_CITY else "bairro", lat, lon, None, json.dumps({"t": place}), rank),
            )
            if place in NEAR_CITY:
                cities.add(lat, lon, name)
            elif place in NEAR_DISTRICT:
                districts.add(lat, lon, name)
            continue

        highway = tags.get("highway")
        if highway and obj.is_way():
            if not name or highway not in STREET_RANK:
                continue
            key = (name.lower(), int(lat / 0.02), int(lon / 0.02))
            if key in seen_streets:
                continue
            seen_streets.add(key)
            stage.execute(
                "INSERT INTO stage VALUES (?,?,?,?,?,?,?,?)",
                (name, tags.get("ref", ""), "rua", lat, lon, None, None, STREET_RANK[highway]),
            )
            continue

        cat = category(tags)
        if not cat:
            continue
        label = name or tags.get("brand") or tags.get("operator") or ""
        stage.execute(
            "INSERT INTO stage VALUES (?,?,?,?,?,?,?,?)",
            (label, alt_names(tags), cat, lat, lon, None, json.dumps(info(tags), ensure_ascii=False) if info(tags) else None,
             CATEGORY_RANK[cat] + (5 if name else -10)),
        )
    db.commit()
    print(f"  OSM: {n:,} objetos em {time.time() - t0:.0f}s", flush=True)


def read_geonames(src: str, db: sqlite3.Connection):
    countries = {}
    with open(os.path.join(src, "countryInfo.txt"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            countries[cols[0]] = cols[4]
    regions = {}
    with open(os.path.join(src, "admin1CodesASCII.txt"), encoding="utf-8") as f:
        for line in f:
            code, name, *_ = line.rstrip("\n").split("\t")
            regions[code] = name
    stage = db.cursor()
    n = 0
    with zipfile.ZipFile(os.path.join(src, "cities1000.zip")) as z:
        with z.open("cities1000.txt") as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                c = line.rstrip("\n").split("\t")
                cc = c[8]
                # All names (incl. Portuguese, e.g. "Nova Iorque") for big cities; a few for small ones.
                alts = [a for a in c[3].split(",") if a][: None if int(c[14] or 0) >= 100_000 else 15]
                near = ", ".join(x for x in (regions.get(f"{cc}.{c[10]}"), countries.get(cc, cc)) if x)
                stage.execute(
                    "INSERT INTO stage VALUES (?,?,?,?,?,?,?,?)",
                    (c[1], " ".join({c[2], *alts} - {c[1]}), "cidade", float(c[4]), float(c[5]), near,
                     json.dumps({"t": "city", "pop": int(c[14] or 0)}), pop_rank(30, c[14])),
                )
                n += 1
    db.commit()
    print(f"  GeoNames: {n:,} cidades", flush=True)


AIRPORT = {"large_airport": ("aeroporto", 90), "medium_airport": ("aeroporto", 70), "small_airport": ("aeroporto", 40),
           "heliport": ("heliporto", 30), "seaplane_base": ("aeroporto", 25)}


def read_airports(src: str, db: sqlite3.Connection):
    countries = {}
    with open(os.path.join(src, "countryInfo.txt"), encoding="utf-8") as f:
        for line in f:
            if not line.startswith("#"):
                cols = line.split("\t")
                countries[cols[0]] = cols[4]
    stage = db.cursor()
    n = 0
    with open(os.path.join(src, "airports.csv"), encoding="utf-8", newline="") as f:
        for a in csv.DictReader(f):
            kind = AIRPORT.get(a["type"])
            if not kind:
                continue
            codes = [x for x in (a["iata_code"], a["icao_code"] or a["gps_code"] or a["ident"]) if x]
            data = {"t": a["type"]}
            if codes:
                data["code"] = " / ".join(codes)
            if a["elevation_ft"]:
                data["elev"] = round(float(a["elevation_ft"]) * 0.3048)
            near = ", ".join(x for x in (a["municipality"], countries.get(a["iso_country"], a["iso_country"])) if x)
            stage.execute(
                "INSERT INTO stage VALUES (?,?,?,?,?,?,?,?)",
                (a["name"], " ".join(codes + ([a["keywords"]] if a["keywords"] else [])), kind[0],
                 float(a["latitude_deg"]), float(a["longitude_deg"]), near, json.dumps(data), kind[1]),
            )
            n += 1
    db.commit()
    print(f"  OurAirports: {n:,} aeroportos e helipontos", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--osm", help="Geofabrik .osm.pbf of one region")
    ap.add_argument("--region", help="region id from app/src/lib/regions.json (with --osm)")
    ap.add_argument("--world", help="folder with the GeoNames and OurAirports files")
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--index",
        default="flex_mem",
        help="osmium node index; big extracts on small machines: sparse_file_array,<file on disk>",
    )
    args = ap.parse_args()
    if bool(args.osm) == bool(args.world):
        ap.error("use --osm (one region) or --world (cities and airports of the world)")
    out = args.out
    if os.path.exists(out):
        os.remove(out)
    tmp = out + ".stage"
    if os.path.exists(tmp):
        os.remove(tmp)
    stage_db = sqlite3.connect(tmp)
    stage_db.execute("PRAGMA journal_mode=OFF")
    stage_db.execute("PRAGMA synchronous=OFF")
    stage_db.execute(
        "CREATE TABLE stage (name TEXT, alt TEXT, cat TEXT, lat REAL, lon REAL, near TEXT, info TEXT, rank INTEGER)"
    )
    cities, districts = Grid(0.3), Grid(0.05)
    if args.osm:
        print(f"Lendo OpenStreetMap ({os.path.basename(args.osm)}, demora)...", flush=True)
        read_osm(args.osm, stage_db, cities, districts, args.index)
        region, sources = args.region or "regiao", "OpenStreetMap (ODbL)"
    else:
        print("Lendo GeoNames...", flush=True)
        read_geonames(args.world, stage_db)
        print("Lendo OurAirports...", flush=True)
        read_airports(args.world, stage_db)
        region, sources = "mundo", "GeoNames (CC BY 4.0), OurAirports (domínio público)"

    print("Montando o índice...", flush=True)
    db = sqlite3.connect(out)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE places (id INTEGER PRIMARY KEY, name TEXT NOT NULL, cat TEXT NOT NULL,
          lat REAL NOT NULL, lon REAL NOT NULL, near TEXT, info TEXT, rank INTEGER NOT NULL);
        CREATE VIRTUAL TABLE places_fts USING fts5(name, alt, near, content='',
          tokenize='unicode61 remove_diacritics 2', prefix='2 3');
        """
    )
    # Insert by importance: FTS returns rowids in ascending order, so a capped query keeps the best.
    rows = stage_db.execute("SELECT name, alt, cat, lat, lon, near, info, rank FROM stage ORDER BY rank DESC")
    batch = []
    rowid = 0
    for name, alt, cat, lat, lon, near, data, rank in rows:
        if near is None:
            city = cities.nearest(lat, lon, 30)
            district = districts.nearest(lat, lon, 3) if cat not in ("cidade", "bairro") else None
            near = ", ".join(x for x in (district, city) if x and x != name) or None
        rowid += 1
        batch.append((rowid, name, cat, round(lat, 6), round(lon, 6), near, data, rank, alt or ""))
        if len(batch) >= 50_000:
            flush(db, batch)
            batch = []
    flush(db, batch)
    db.execute("CREATE INDEX places_cat ON places(cat, lat)")
    db.execute("INSERT INTO places_fts(places_fts) VALUES ('optimize')")
    db.executemany(
        "INSERT INTO meta VALUES (?,?)",
        [("schema", str(SCHEMA_VERSION)), ("built", time.strftime("%Y-%m-%d")), ("count", str(rowid)),
         ("region", region), ("sources", sources)],
    )
    db.commit()
    stage_db.close()
    os.remove(tmp)
    db.execute("VACUUM")
    db.close()
    print(f"Pronto: {out} ({rowid:,} lugares, {os.path.getsize(out) / 1e6:.0f} MB)", flush=True)


def flush(db, batch):
    db.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?,?)", [b[:8] for b in batch])
    db.executemany(
        "INSERT INTO places_fts(rowid, name, alt, near) VALUES (?,?,?,?)", [(b[0], b[1], b[8], b[5] or "") for b in batch]
    )


if __name__ == "__main__":
    main()
