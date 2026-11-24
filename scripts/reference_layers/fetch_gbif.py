"""Fetch GBIF occurrences of threatened species (IUCN CR and EN) in the nine Amazon countries.

    python scripts/reference_layers/fetch_gbif.py OUT_DIR

Writes OUT_DIR/<country>.geojsonl, one point per record, for build_pmtiles.sh species.
Uses the public search API (no account), one query per country and year since 2000, inside a
box around the Amazon. Paging deep into one big query gets slow, and the search API stops at
100,000 records per query; one year per query keeps both small. A country file that already
exists is skipped, so a stopped run can be started again.
ponytail: search API pages, fine for ~150k records. For a citable dataset (a DOI) or VU as
well, use the GBIF download API with an account and feed its CSV to the same build step.
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.gbif.org/v1/occurrence/search"
COUNTRIES = ("BO", "BR", "CO", "EC", "GF", "GY", "PE", "SR", "VE")
BOX = "POLYGON((-80 -20,-44 -20,-44 6,-80 6,-80 -20))"
PAGE = 300
KEEP = ("species", "family", "class", "iucnRedListCategory", "year", "basisOfRecord", "license", "datasetKey")


def query(country: str, year: int, offset: int) -> dict:
    params = [
        ("country", country), ("geometry", BOX), ("iucnRedListCategory", "CR"), ("iucnRedListCategory", "EN"),
        ("hasCoordinate", "true"), ("hasGeospatialIssue", "false"), ("occurrenceStatus", "PRESENT"),
        ("year", str(year)), ("limit", str(PAGE)), ("offset", str(offset)),
    ]
    url = f"{API}?{urllib.parse.urlencode(params)}"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "geotalos-reference-layers"}), timeout=60) as r:
                return json.load(r)
        except OSError:
            time.sleep(2**attempt)
    raise RuntimeError(f"GBIF did not answer: {url}")


def feature(rec: dict) -> dict | None:
    lon, lat = rec.get("decimalLongitude"), rec.get("decimalLatitude")
    if lon is None or lat is None:
        return None
    props = {k: rec.get(k) for k in KEEP}
    props["category"] = props.pop("iucnRedListCategory")
    props["gbif_id"] = rec.get("key")
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for c in COUNTRIES:
        target = out / f"{c}.geojsonl"
        if target.exists():
            print(f"{c}: already there, skipped", flush=True)
            continue
        n = 0
        with open(target.with_suffix(".part"), "w") as f:
            for year in range(2000, time.gmtime().tm_year + 1):
                offset = 0
                while True:
                    page = query(c, year, offset)
                    for rec in page["results"]:
                        feat = feature(rec)
                        if feat:
                            f.write(json.dumps(feat, ensure_ascii=False) + "\n")
                            n += 1
                    offset += PAGE
                    if page.get("endOfRecords") or offset >= 100_000:
                        break
                    time.sleep(0.2)  # be polite to the public API
        target.with_suffix(".part").rename(target)
        print(f"{c}: {n} records", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
