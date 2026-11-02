#!/usr/bin/env bash
# Load the Ramsar Sites boundaries into PostGIS (reference.ramsar), served live by Martin.
#
#   scripts/reference_layers/load_ramsar.sh /path/to/ramsar_sites_global.zip
#
# Source: Ramsar Sites Information Service, global WFS shapefile export (CC BY 4.0)
#   https://rsis.ramsar.org/geoserver/wfs?request=GetFeature&service=wfs&version=1.0.0&typename=ramsar_sdi%3Afeatures_published&outputformat=SHAPE-ZIP
#
# Needs the stack running (app-db up, migration 074 applied) and Docker. Safe to rerun:
# the table is emptied first.
set -euo pipefail

ZIP=$(realpath "${1:?usage: load_ramsar.sh <ramsar_sites_global.zip>}")
cd "$(dirname "$0")/../.."
GDAL=${GDAL_IMAGE:-ghcr.io/osgeo/gdal:ubuntu-small-3.9.2}
NETWORK=${COMPOSE_NETWORK:-awakeforest-backend}
set -a; [[ -f .env ]] && source .env; set +a   # APP_DB_SUPERUSER_PASSWORD
PGPASS=${APP_DB_SUPERUSER_PASSWORD:-postgres}
psql() { docker compose exec -T app-db psql -v ON_ERROR_STOP=1 -U postgres -d geoplat "$@"; }

psql -c "TRUNCATE reference.ramsar RESTART IDENTITY"

# The published polygons only (features_published_NULL has no geometry and every
# site in it also has polygons). The shapefile is ISO-8859-1 although GDAL does not
# read the .cst; 103 rows have invalid rings, so -makevalid. Keep only the fields
# the map shows. Loaded into a staging table first: ogr2ogr cannot map the geometry
# of a source layer onto an existing table's `geom` column when appending.
docker run --rm --network "$NETWORK" -v "$ZIP":/data/ramsar.zip:ro "$GDAL" \
  ogr2ogr -f PostgreSQL "PG:host=app-db dbname=geoplat user=postgres password=$PGPASS" \
  "/vsizip//data/ramsar.zip/features_publishedPolygon.shp" \
  -oo ENCODING=ISO-8859-1 -makevalid -nlt PROMOTE_TO_MULTI \
  -select ramsarid,officialna,iso3,country_en,area_off \
  -nln reference.ramsar_stage -overwrite -lco GEOMETRY_NAME=geom

psql -c "INSERT INTO reference.ramsar (ramsarid, officialna, iso3, country_en, area_off, geom)
         SELECT ramsarid, officialna, iso3, country_en, area_off, ST_Multi(geom) FROM reference.ramsar_stage"
psql -c "DROP TABLE reference.ramsar_stage"

# Simplified copy (about 1 km) used at zoom 6 and below.
psql -c "UPDATE reference.ramsar SET geom_s = ST_Multi(ST_SimplifyPreserveTopology(geom, 0.01)) WHERE geom_s IS NULL"
psql -c "ANALYZE reference.ramsar"
psql -tAc "SELECT count(*) || ' rows, ' || count(DISTINCT ramsarid) || ' sites' FROM reference.ramsar"
