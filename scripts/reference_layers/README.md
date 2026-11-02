# Public reference layers

Global, read-only layers shown in the map's "Reference layers" panel. They are the same for
every organization and are served by Martin through one allowlisted route,
`GET /api/v1/reference-layers/{name}/tiles/{z}/{x}/{y}.pbf` (`app/api/v1/endpoints/reference_layers.py`).
Only the names in `REFERENCE_LAYERS` are reachable, so the private annotation sources on the
same Martin cannot be.

Two ways to serve a layer:

| Kind | Where it lives | Use for | Scripts |
|---|---|---|---|
| Small | PostGIS table in the `reference` schema (with a GiST index), served live by a Martin function source | Ramsar | `load_ramsar.sh` |
| Large | Tippecanoe PMTiles file in the MinIO `reference-tiles` bucket, read by Martin over S3 | everything else | `build_pmtiles.sh`, `upload_pmtiles.sh` |

## Layers and sources

| Martin id | Layer | Kind | Source (download) |
|---|---|---|---|
| `ramsar` | Ramsar Sites | PostGIS | [RSIS WFS shapefile export](https://rsis.ramsar.org/geoserver/wfs?request=GetFeature&service=wfs&version=1.0.0&typename=ramsar_sdi%3Afeatures_published&outputformat=SHAPE-ZIP), licence [CC BY 4.0](https://www.ramsar.org/licence-information) |
| `raisg_protected_areas` | Amazon protected areas | PMTiles | [RAISG](https://www.raisg.org/en/maps/), [ArcGIS REST](https://geo.socioambiental.org/webadaptor2/rest/services/raisg/) |
| `raisg_mining` | Amazon mining (RAISG) | PMTiles | [RAISG](https://www.raisg.org/en/maps/), [ArcGIS REST](https://geo.socioambiental.org/webadaptor2/rest/services/raisg/) |
| `gfw_mining` | Mining concessions | PMTiles | [GFW dataset](https://data-api.globalforestwatch.org/dataset/gfw_mining_concessions), [public 2025 ZIP](https://gfw2-data.s3.amazonaws.com/concessions/2025/final_layers/public/gfw_global_mining_concessions_v2025_public.zip), CC BY 4.0 |
| `gfw_logging` | Logging concessions | PMTiles | [WRI Resource Watch](https://api.resourcewatch.org/v1/dataset/62c3d70c-d218-458f-8342-ee6a2b0ba434), [Carto table](https://wri-01.carto.com/tables/gfw_logging) |
| `hydrorivers` | HydroRIVERS (South America) | PMTiles | [HydroSHEDS](https://www.hydrosheds.org/products/hydrorivers), [shapefile ZIP](https://data.hydrosheds.org/file/HydroRIVERS/HydroRIVERS_v10_sa_shp.zip) |
| `mrds` | USGS mineral deposits | PMTiles | [USGS MRDS](https://www.usgs.gov/publications/mineral-resources-data-system-mrds), [mrds-trim.zip](https://mrdata.usgs.gov/mrds/mrds-trim.zip), public domain |
| `cofopri` | Peru native communities | PMTiles | [GEOCATMIN](https://geocatmin.ingemmet.gob.pe/), [COFOPRI-MINAGRI layer](https://geocatmin.ingemmet.gob.pe/arcgis/rest/services/SERV_OTRAS_FUENTES/MapServer/45) |

The full list with checksums and licence notes is in `SOURCES.md` and `dataset/README.md` of the
project workspace. Check each publisher's terms before sharing the tiles. Every layer's
attribution is stored in its tileset metadata and shown on the map.

## Rebuild a small layer (Ramsar, PostGIS)

```bash
docker compose up -d app-db app-db-migrate          # migration 074 creates reference.ramsar
scripts/reference_layers/load_ramsar.sh /path/to/ramsar_sites_global.zip
```

Reads `features_publishedPolygon.shp` (ISO-8859-1), repairs the 103 invalid rings, keeps
`ramsarid, officialna, iso3, country_en, area_off`, and fills `geom` plus a simplified `geom_s`
(about 1 km) that is used at zoom 6 and below. Safe to rerun. Expect 7,458 rows and 1,652 sites.
Martin serves it live through `reference.ramsar_mvt()`.

## Rebuild the large layers (PMTiles in MinIO)

```bash
SRC=/path/to/downloads WORK=/path/to/scratch \
  scripts/reference_layers/build_pmtiles.sh              # all seven, or name some
scripts/reference_layers/upload_pmtiles.sh               # data/reference-tiles/*.pmtiles -> MinIO
docker compose restart martin
```

`build_pmtiles.sh` converts each source with GDAL (invalid geometry repaired, only the display
fields kept), then tiles it with Tippecanoe 2.79 (250 KB tile cap, zoom 0 to 12, density-based
thinning at low zoom). `TILE_ONLY=1` reuses the cleaned GeoJSONL and only re-tiles. The `.pmtiles`
files are written to `data/reference-tiles/` (git-ignored). Needs Docker; large layers take
minutes each and a few GB of scratch space.

## Add a layer

1. Build it (`build_pmtiles.sh`, one more `build_<name>` function) or write a SQL function for a small one.
2. Upload it and add it under `pmtiles: sources:` (or `functions:`) in `infra/docker/martin/config.yaml`.
3. Add its id to `REFERENCE_LAYERS`. A layer Martin does not serve yet is skipped in the list and returns 404 for tiles.

## Notes

* Martin reads the bucket as the read-only MinIO user `martin-tiles` (policy `reference-tiles-read`,
  created by `minio-setup`); it never gets the root credentials. Its password is `MARTIN_TILES_PASSWORD`.
* Migration 074 creates the `reference` schema as `app_user`, which needs `CREATE` on the database.
  The init script grants it for new databases. An existing database needs it once:
  `GRANT CREATE ON DATABASE geoplat TO app_user;`
* The migration only creates the empty table and function; the data comes from the load script.
