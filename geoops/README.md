# geoops

Embedding bank + similarity search for class objects (bboxes/masks selected on
a dataset item). Self-contained module — the rest of `app/` only touches it at
four points:

1. `app/api/v1/router.py` mounts `geoops.api.router`.
2. `app/db/_all_models.py` imports `geoops.models` so Alembic sees the
   `embeddings` and `embedding_tiles` tables in `Base.metadata`.
3. Migrations `049`–`055` in `alembic/versions/` create the `vector`
   extension, the `embeddings` table, `embeddings.tile_tier`, the
   `embedding_tiles` table, the `aoi_scan`/`anomaly_detection` `JobType`s, and
   `embeddings.created_by_job_id` + its `(annotation_id, model_id)` cache
   constraint.
4. `app/workers/celery_app.py` registers `geoops.tasks` (`include` +
   `task_routes`, routed to the `embedding` queue in `app/workers/queues.py`)
   so `geoops.tasks.run_aoi_scan` runs as a normal Celery task. In
   `docker-compose.yml` the `embedding` queue is consumed by the existing
   `celery-worker-inference` container (`--queues=inference,embedding`) rather
   than its own service — same profile (HTTP-bound calls to an external model
   endpoint) and it already has the `host.docker.internal` network access
   needed to reach one.

Everything else — the ORM models, schemas, and service logic — lives in this
package and only reaches into `app/` for things it has no reason to
duplicate: the DB `Base`, the `AIModel`/`DatasetItem`/`Annotation`/
`AnnotationClass`/`Job` models it references by FK, the RLS-aware session
dependency, `app.services.titiler_service.get_item_bbox_preview` for cropping
a patch image out of a STAC item (async, request path), and the same
sync-`urllib` TiTiler/model-call pattern `app.services.model_manager` uses
for worker-side HTTP (`geoops/tasks.py`).

## Model

Each `ai_models` row can be an *embedding* model — same table used for
inference models, just pointed at an endpoint that returns
`{model_name, embedding_dim, embedding}` instead of predictions. Nothing new
to configure there.

## Two tables — curated bank vs. scan cache

- **`embeddings`** — one row per deliberate user/annotation-driven selection,
  *or* one row per model-generated annotation embedded in bulk by anomaly
  detection (`created_by_job_id` set in that case). This is the only table
  `search()` reads. Nullable `class_id`/`annotation_id` distinguish a manual
  bbox pick from one tied to a saved `Annotation`. A partial unique index on
  `(annotation_id, model_id)` (where `annotation_id IS NOT NULL`) means one
  annotation only ever needs one embedding per model — both the manual create
  endpoint and the anomaly-detection job check-then-reuse against it rather
  than ever duplicating.
- **`embedding_tiles`** — a cache of grid-aligned patches produced by AOI
  scans. Never read by `search()`, never a "curated" result on its own; it
  exists purely so a scan doesn't re-embed the same geographic tile twice.
  Every row is tagged with `created_by_job_id` and keyed by
  `(dataset_item_id, model_id, tile_tier, tile_col, tile_row)` — a unique
  constraint that doubles as the cache lookup.

Splitting these avoids two problems a single table would have: `search()`
having to filter scan noise out of the bank, and scan tiles bloating a table
meant to hold deliberate, human-curated selections.

## Scale tiers (`geoops/scale.py`)

Every embedding is a crop resized to the same `crop_size_px` pixel grid
regardless of its real-world size — a 5m object and a 500m object cropped to
the same 256×256 patch encode completely different things. `tile_tier` snaps
an embedding's source-geometry bbox span (in meters, via a simple
equirectangular approximation — no PostGIS needed) to one of
`DEFAULT_TIERS_M = [10, 40, 160, 640, 2560]` meters. `search()` only compares
same-tier candidates; AOI scans tile a dataset item at the reference's own
tier, anchored to the item's own bbox origin so the same `(item, tier, col,
row)` always maps to the same geographic tile across separate scans.

Sanity-check `DEFAULT_TIERS_M` once the DB is reachable:

```sql
WITH bbox AS (
  SELECT a.class_id, ac.name AS class_name,
         ST_XMin(a.geometry) minx, ST_XMax(a.geometry) maxx,
         ST_YMin(a.geometry) miny, ST_YMax(a.geometry) maxy
  FROM annotations a JOIN annotation_classes ac ON ac.id = a.class_id
  WHERE a.deleted_at IS NULL
), meters AS (
  SELECT class_name,
    ST_Distance(ST_SetSRID(ST_MakePoint(minx,(miny+maxy)/2),4326)::geography,
                ST_SetSRID(ST_MakePoint(maxx,(miny+maxy)/2),4326)::geography) width_m,
    ST_Distance(ST_SetSRID(ST_MakePoint((minx+maxx)/2,miny),4326)::geography,
                ST_SetSRID(ST_MakePoint((minx+maxx)/2,maxy),4326)::geography) height_m
  FROM bbox
)
SELECT class_name, count(*),
  percentile_cont(0.5) WITHIN GROUP (ORDER BY GREATEST(width_m,height_m)) p50_m,
  percentile_cont(0.99) WITHIN GROUP (ORDER BY GREATEST(width_m,height_m)) p99_m
FROM meters GROUP BY class_name ORDER BY p50_m;
```

## Flow

- `POST /api/v1/embeddings` — crop the selected bbox/mask out of a dataset
  item, POST it to the embedding model's endpoint, store the returned vector
  in `embeddings` with its computed `tile_tier`. Synchronous (single object,
  low latency).
- `POST /api/v1/embeddings/search` — cosine nearest-neighbours over
  `embeddings`, scoped to the reference's `model_name` **and** `tile_tier`
  (never compares across models or across wildly different real-world
  scales), plus org.
- `POST /api/v1/embeddings/aoi-scan` — **async Celery job** (returns 202 +
  `job_id`; poll `GET /api/jobs/{id}`). Tiles the AOI at the reference's own
  scale tier, checks `embedding_tiles` for tiles already cached from a prior
  scan, embeds only the misses (capped by `max_patches`), ranks all
  candidates (cached + new) by cosine similarity, writes the result to
  `job.config["result"]`. See `geoops/tasks.py::run_aoi_scan`.
- `POST /api/v1/embeddings/anomaly-scan` — **async Celery job** (returns 202 +
  `job_id`; poll `GET /api/jobs/{id}`). Unlike `aoi-scan`, this never tiles
  empty space: it only embeds annotations a model already produced
  (`AnnotationSet.source_type == 'model'`) for one requested `class_id` across
  the given `dataset_item_ids` (optionally further clipped by `aoi_bbox`).
  Already-embedded annotations are reused via the `(annotation_id, model_id)`
  cache. Candidates are grouped by `tile_tier` and, for any group with at
  least `min_group_size` members, scored by cosine distance to that group's
  centroid — annotations least like their peers rank first. Groups too small
  to score are still reported (`scored: false`). See
  `geoops/tasks.py::run_anomaly_detection`.

## v1 scope, on purpose

No ANN index (HNSW/IVFFlat) yet, no whole-dataset bulk-indexing job (deferred
— `aoi_scan`'s own cache is the only persistence today). The `embedding`
column is unconstrained (`vector`, no fixed dimension) because different
embedding models can return different dimensions — every query filters by
`model_name` first so cosine distance never compares mismatched dimensions.
Revisit once one model/dimension is the standard and volume justifies an
index.
