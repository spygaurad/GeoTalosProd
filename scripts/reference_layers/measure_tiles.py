"""Measure reference layer tile weight over one area, per zoom.

  python3 scripts/reference_layers/measure_tiles.py [--api http://localhost:2024] [--out before.json] layer ...

Fetches every tile covering BBOX at each zoom (gzip on, like a browser) and prints
tile count, total and largest tile size, and the slowest response. Default area is
Cusco and Madre de Dios, the view in Suraj's 19 Nov screenshot.
"""
import argparse, json, math, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

BBOX = (-73.5, -15.5, -68.5, -11.5)  # west, south, east, north
ZOOMS = range(5, 13)


def tile_range(z, west, south, east, north):
    n = 2 ** z
    def xy(lon, lat):
        x = int((lon + 180) / 360 * n)
        y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
        return min(x, n - 1), min(y, n - 1)
    x0, y0 = xy(west, north)
    x1, y1 = xy(east, south)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def fetch(url):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req) as r:
            n = len(r.read())
    except urllib.error.HTTPError as e:  # 404 below a layer's min zoom: nothing is sent
        if e.code != 404:
            raise
        n = 0
    return n, (time.perf_counter() - t) * 1000


def measure(api, layer, max_tiles=400):
    rows = []
    for z in ZOOMS:
        tiles = tile_range(z, *BBOX)
        if len(tiles) > max_tiles:  # ponytail: caps very high zooms; raise max_tiles for a full sweep
            step = len(tiles) // max_tiles + 1
            tiles = tiles[::step]
        urls = [f"{api}/api/v1/reference-layers/{layer}/tiles/{z}/{x}/{y}.pbf" for x, y in tiles]
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(fetch, urls))
        sizes = [s for s, _ in res]
        rows.append({"z": z, "tiles": len(res), "total_kb": round(sum(sizes) / 1024, 1),
                     "max_kb": round(max(sizes) / 1024, 1), "slowest_ms": round(max(ms for _, ms in res))})
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("layers", nargs="+")
    ap.add_argument("--api", default="http://localhost:2024")
    ap.add_argument("--out")
    a = ap.parse_args()
    result = {}
    for layer in a.layers:
        result[layer] = measure(a.api, layer)
        print(f"\n{layer}\n  z  tiles  total_kB  max_kB  slowest_ms")
        for r in result[layer]:
            print(f"  {r['z']:>2} {r['tiles']:>6} {r['total_kb']:>9} {r['max_kb']:>7} {r['slowest_ms']:>10}")
    if a.out:
        json.dump(result, open(a.out, "w"), indent=1)
