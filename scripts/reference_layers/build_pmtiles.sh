#!/usr/bin/env bash
# Build the large public reference layers as PMTiles with Tippecanoe.
#
#   SRC=/path/to/source/downloads WORK=/path/to/scratch OUT=/path/to/out \
#     scripts/reference_layers/build_pmtiles.sh [name ...]
#   TILE_ONLY=1 ... build_pmtiles.sh <name>   reuse the cleaned GeoJSONL in WORK, re-tile only
#
# Layers: cofopri raisg_protected_areas mrds gfw_logging hydrorivers raisg_mining gfw_mining
# (Ramsar is small and lives in PostGIS: see load_ramsar.sh.) Source links: README.md.
# SRC is laid out like the publishers' zips: usgs_mrds/, hydrorivers/, gfw_logging/,
# gfw_mining/, raisg/{protected_areas,mining}/, indigenous_registries/peru/cofopri_minagri/.
# Needs Docker (GDAL and Tippecanoe images). The output .pmtiles go to OUT, then
# upload_pmtiles.sh puts them in the MinIO reference-tiles bucket.
set -euo pipefail
SRC=${SRC:?set SRC to the folder with the source downloads}
WORK=${WORK:?set WORK to a scratch folder for the cleaned GeoJSONL}
OUT=${OUT:-"$(dirname "$0")/../../data/reference-tiles"}
mkdir -p "$OUT"; OUT=$(realpath "$OUT")
GDAL=ghcr.io/osgeo/gdal:ubuntu-small-3.9.2
TIP=${TIPPECANOE_IMAGE:-local/tippecanoe:2.79.0}  # built from felt/tippecanoe tag 2.79.0
run() { docker run --rm -u "$(id -u):$(id -g)" -v "$SRC":/data:ro -v "$WORK":/work -v "$OUT":/out -w /work "$@"; }
z() { echo "/vsizip//data/$1/$2"; }
CC='(<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>), modified'

# convert NAME OUT SRC FIELDS [extra ogr2ogr args]
convert() {
  [[ -n ${TILE_ONLY:-} ]] && return
  local name=$1 out=$2 src=$3 fields=$4; shift 4
  run $GDAL ogr2ogr -f GeoJSONSeq "/work/$name/$out" "$src" -makevalid -select "$fields" \
    -nln "$name" -lco COORDINATE_PRECISION=15 -oo OGR_GEOJSON_MAX_OBJ_SIZE=0 "$@" 2>&1 | grep -v 'OGR_GEOJSON_MAX_OBJ_SIZE' || true
}
# tile NAME TITLE DESC ATTRIB MAXZOOM [extra tippecanoe args]
tile() {
  local name=$1 title=$2 desc=$3 attrib=$4 maxz=$5 f inputs=(); shift 5
  for f in "$WORK/$name"/*.geojsonl; do inputs+=("/work/$name/${f##*/}"); done
  # 250 KB gzipped tile cap (the 500 KB default is too heavy for a browser at low zoom)
  run $TIP tippecanoe -q -o "/out/$name.pmtiles" --force -l "$name" -n "$title" -N "$desc" \
    --attribution="$attrib" -Z0 -z"$maxz" --drop-densest-as-needed -M 250000 -P "$@" "${inputs[@]}"
}

build_mrds() {
  convert mrds mrds.geojsonl "$(z usgs_mrds/mrds-trim.zip mrds-trim.shp)" DEP_ID,SITE_NAME,DEV_STAT,CODE_LIST
  # -r1: keep every point eligible at every zoom; the cap thins the densest areas
  tile mrds "USGS Mineral Deposits (MRDS)" "Mineral Resources Data System, global mineral sites" \
    '<a href="https://mrdata.usgs.gov/mrds/">USGS Mineral Resources Data System</a> (public domain)' 12 -r1 --extend-zooms-if-still-dropping
}
build_hydrorivers() {
  convert hydrorivers hydrorivers.geojsonl "$(z hydrorivers/HydroRIVERS_v10_sa_shp.zip HydroRIVERS_v10_sa_shp/HydroRIVERS_v10_sa.shp)" \
    HYRIV_ID,MAIN_RIV,ORD_STRA,ORD_FLOW,DIS_AV_CMS,LENGTH_KM
  # Strahler order gate per zoom: only big rivers when zoomed out, all at z10.
  tile hydrorivers "HydroRIVERS (South America)" "River network, 15 arc-second, v1.0" \
    '<a href="https://www.hydrosheds.org/products/hydrorivers">HydroRIVERS v1.0</a> &copy; WWF HydroSHEDS (Lehner &amp; Grill 2013)' 10 \
    -j '{"*":["any",[">=","ORD_STRA",8],["all",[">=","$zoom",3],[">=","ORD_STRA",7]],["all",[">=","$zoom",5],[">=","ORD_STRA",6]],["all",[">=","$zoom",6],[">=","ORD_STRA",5]],["all",[">=","$zoom",7],[">=","ORD_STRA",4]],["all",[">=","$zoom",8],[">=","ORD_STRA",3]],["all",[">=","$zoom",9],[">=","ORD_STRA",2]],[">=","$zoom",10]]}'
}
build_gfw_logging() {
  convert gfw_logging gfw_logging.geojsonl "$(z gfw_logging/gfw_logging_concessions_2025.zip gfw_logging_concessions_2025.shp)" \
    gfwid,name,company,country,legal_term,status,area_ha -nlt PROMOTE_TO_MULTI
  tile gfw_logging "Logging Concessions (GFW)" "Managed forest concessions, 2025" \
    '<a href="https://www.globalforestwatch.org">Logging concessions 2025, WRI / Global Forest Watch</a>' 12 --extend-zooms-if-still-dropping
}
build_gfw_mining() {
  local i
  for i in 1 2; do
    convert gfw_mining gfw_mining_pt$i.geojsonl "$(z gfw_mining/gfw_global_mining_concessions_v2025_public.zip gfw_global_mining_concessions_v2025_public_pt$i.shp)" \
      iso3,conc_name,conc_type,conc_stat,company,mineral,source -nlt PROMOTE_TO_MULTI -dim XY
  done
  tile gfw_mining "Mining Concessions (GFW)" "Mining concessions, 2014-2025, select countries" \
    "<a href=\"https://www.globalnaturewatch.org\">Mining concessions, WRI / Global Nature Watch</a> $CC" 12 --extend-zooms-if-still-dropping
}
build_raisg_protected_areas() {
  local f
  for f in "$SRC"/raisg/protected_areas/part-*.geojson; do f=${f##*/}
    convert raisg_protected_areas "${f%.geojson}.geojsonl" "/data/raisg/protected_areas/$f" \
      objectid,pais,nombre,categoria,categoria_uicn,status,area_oficial_ha -nlt PROMOTE_TO_MULTI
  done
  tile raisg_protected_areas "Amazon Protected Areas (RAISG)" "National protected areas in the Amazon region" \
    '<a href="https://www.raisg.org/en/maps/">RAISG</a> (Red Amazónica de Información Socioambiental Georreferenciada)' 12 --extend-zooms-if-still-dropping
}
build_raisg_mining() {
  local f
  for f in "$SRC"/raisg/mining/part-*.geojson; do f=${f##*/}
    convert raisg_mining "${f%.geojson}.geojsonl" "/data/raisg/mining/$f" \
      objectid,pais,nombre,cia,situacion,tipo_minerio,area_sig_ha -nlt PROMOTE_TO_MULTI
  done
  tile raisg_mining "Amazon Mining (RAISG)" "Mining claims and concessions in the Amazon region" \
    '<a href="https://www.raisg.org/en/maps/">RAISG</a> (Red Amazónica de Información Socioambiental Georreferenciada)' 12 --extend-zooms-if-still-dropping
}
build_cofopri() {
  local f
  for f in "$SRC"/indigenous_registries/peru/cofopri_minagri/part-*.geojson; do f=${f##*/}
    convert cofopri "${f%.geojson}.geojsonl" "/data/indigenous_registries/peru/cofopri_minagri/$f" \
      OBJECTID,NOMBRE,NOMDEP,NOMPRO,NOMDIS,TIPCOM,AREHAS -nlt PROMOTE_TO_MULTI
  done
  tile cofopri "Peru Native Communities (COFOPRI)" "Comunidades nativas, COFOPRI / MINAGRI" \
    '<a href="https://geocatmin.ingemmet.gob.pe/">Comunidades nativas, COFOPRI / MINAGRI Peru (GEOCATMIN)</a>' 12 --extend-zooms-if-still-dropping
}

names=("${@:-cofopri raisg_protected_areas mrds gfw_logging hydrorivers raisg_mining gfw_mining}")
for n in ${names[@]}; do
  mkdir -p "$WORK/$n"; [[ -n ${TILE_ONLY:-} ]] || rm -f "$WORK/$n"/*.geojsonl
  t0=$SECONDS; echo "=== $n start $(date +%T)"
  "build_$n"
  echo "=== $n done in $((SECONDS - t0))s, $(du -h "$OUT/$n.pmtiles" | cut -f1)"
done
echo "ALL BUILDS FINISHED"
