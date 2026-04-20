# Segmentation Mask Implementation — Critical Issues Analysis

**Status:** AwakeForest feature/setup_basemap_martin branch  
**Date:** 2026-04-15  
**Focus:** Ingestion → Annotation Sets → TiTiler Streaming

---

## I. Correct Architecture (User Clarification)

**IMPORTANT:** Martin is **NOT being used** because Leaflet doesn't support its vector tile format. The actual intent is:

1. **Ingestion Layer** (`app/workers/ingestion/rasterio_utils.py`)
   - Extracts COG metadata, previews unique **class ID pixel values**
   - `extract_unique_values()`: decimates raster, returns unique pixel values (the class IDs)

2. **Annotation Set Layer** (`app/api/v1/endpoints/annotation_sets.py`)
   - User maps pixel value (class ID) → annotation class UUID
   - Server-side stores: `value_class_map: {class_id: class_uuid}` + `colormap: {class_id: [R,G,B,A]}`
   - Martin endpoints (Issue #3, #12) are **DEAD CODE** — can be removed

3. **TiTiler Streaming Layer** (`app/services/titiler_service.py`)
   - **THIS IS WHERE IT SHOULD WORK**: TiTiler can apply colormaps to raster tiles
   - **Missing:** Proper endpoint that:
     - Reads stored `value_class_map` and `colormap` from DB
     - Passes colormap to TiTiler at request time
     - Streams colored raster tiles via `/tiles/collections/{cid}/items/{iid}/{z}/{x}/{y}.png`
   - **Missing:** Frontend UI to easily select and overlay segmentation masks on map

4. **Frontend Visualization** (MISSING)
   - Easy dropdown/list to select which segmentation mask to view
   - Overlay toggle, opacity control
   - Legend showing class IDs + class names + colors

---

## II. Critical Issues by Component

### Issue 1: **Segmentation Masks Stored But Rendering Logic Incomplete**

**Location:** `app/api/v1/endpoints/annotation_sets.py` lines 567–647, `app/api/v1/endpoints/tiles.py` lines 137–150  
**Severity:** CRITICAL  
**Root Cause:** Config is saved but tile endpoint doesn't use it

**What currently happens:**
```python
# Line 606–616: Store raster mask config in map_layer.source_config
source_config["raster_mask"] = {
    "dataset_item_id": str(item.id),
    "stac_collection_id": item.stac_collection_id,
    "stac_item_id": item.stac_item_id,
    "band_index": payload.band_index,
    "nodata_value": payload.nodata_value,
    "value_class_map": {k: str(v) for k, v in value_class_map.items()},
}
# Line 645: Colormap built from class styles and returned to frontend
colormap = await _build_colormap(...)  # e.g., {"1": [255,0,0,255], "2": [0,255,0,255]}
```

**Returned tile URL template** (line 630–633):
```
/api/v1/tiles/collections/{collection_id}/items/{item_id}/{z}/{x}/{y}.png
```

**Problem:**
- Tile endpoint `/tiles/collections/{cid}/items/{iid}/{z}/{x}/{y}.{fmt}` (tiles.py:137) does **NOT know about the stored raster config**
- It just forwards to TiTiler with no colormap information
- Colormap is returned to frontend once, but never used during tile requests
- **Result:** Raster mask renders as raw pixel values (class IDs as grayscale), not colored by class style

---

### Issue 2: **Colormap Computed But Not Applied to Tile Requests**

**Location:** `app/api/v1/endpoints/annotation_sets.py` lines 176–203, `app/api/v1/endpoints/tiles.py` lines 137–150  
**Severity:** CRITICAL  
**Root Cause:** Tile endpoint has no way to access stored colormap config

**Current state:**
1. Colormap IS computed server-side from class styles (line 592)
2. Colormap IS returned to frontend (line 645)
3. Colormap IS stored in `map_layer.source_config["raster_mask"]` (line 614)
4. **BUT:** Generic tile endpoint doesn't read or use this config
5. **BUT:** Documentation tells frontend to send colormap as URL param (user says this is wrong)

**The real problem:**
- Tile endpoint `/tiles/collections/{cid}/items/{iid}/{z}/{x}/{y}.png` doesn't know:
  - Which annotation set this raster belongs to
  - What the `value_class_map` is
  - What the colormap should be
- It just proxies to TiTiler with default params
- If class styles change, tiles don't automatically re-color (cached old version)

**Needed:** Tile endpoint must:
1. Load raster config from `annotation_set` or `map_layer`
2. Build colormap on-the-fly OR use cached colormap
3. Pass colormap to TiTiler with every request

---

### Issue 3: **Martin Vector Tile Endpoints Are Dead Code** ⚠️ **TO REMOVE**

**Location:** `app/api/v1/endpoints/annotation_sets.py` lines 763–820  
**Severity:** LOW (can be deleted)  
**Status:** Not used; Leaflet doesn't support Martin PBF format

**Dead code:**
```python
# Line 763: /annotation-sets/{set_id}/tiles/{z}/{x}/{y}.pbf
# Proxies to Martin's annotation_set_mvt (vector tile function)
# ← NEVER CALLED; not integrated into frontend
```

**Clarification from user:**
- Martin tiles are **not being used** because Leaflet doesn't support PBF vector tile format
- Segmentation masks render via TiTiler as raster tiles, **not** Martin MVT
- Annotation set vector tiles (GeoJSON) are also **not implemented** via Martin

**Action:** Remove lines 763–820 and the Martin client setup (lines 32–40, 86–40) to reduce confusion.

---

### Issue 4: **No RLS Enforcement on Segmentation Mask Access**

**Location:** `app/services/titiler_service.py`  
**Severity:** CRITICAL  
**Root Cause:** Generic tile endpoints don't validate org/project membership

**Analysis:**
1. `/tiles/collections/{cid}/items/{iid}/{z}/{x}/{y}.png` endpoints (lines 137–150 in tiles.py) only check `require_org_role("org:viewer")`
2. They **do NOT verify** that the organization owns the dataset or dataset item
3. Malicious org member can request tiles for any collection/item by guessing IDs
4. **Cross-org leakage risk:** No query to `SELECT … WHERE organization_id = ?`

**Code gap:**
```python
# tiles.py line 137–150: No DB check
@router.get("/collections/{collection_id}/items/{item_id}/{z}/{x}/{y}.{fmt}")
async def proxy_item_tile(
    collection_id: str,
    item_id: str,
    ...
    _db: Any = Depends(get_session),  # Passed but never used
):
    # Missing:
    # SELECT * FROM dataset_items 
    # WHERE stac_item_id = item_id AND organization_id = org_id
```

---

### Issue 5: **Pixel Value Extraction Not Production-Safe**

**Location:** `app/workers/ingestion/rasterio_utils.py` lines 720–760  
**Severity:** HIGH  
**Root Cause:** Unbounded memory read for "unique values"

**Current code:**
```python
def extract_unique_values(
    s3_uri: str,
    gdal_env: dict,
    band_index: int = 1,
    max_pixels: int = 512 * 512,  # Read 256k pixels
    max_values: int = 256,         # Return top 256 unique values
) -> dict:
    """Decimate raster, find unique pixel values."""
    # Reads max_pixels pixels into memory
    # Returns up to max_values unique values
```

**Problems:**
1. For a 1-band uint32 raster with 256k pixels, could return 256k unique values
2. Frontend then sends each value → colormap as URL parameter
3. **URL length limit:** 2048 chars in many browsers/proxies
4. **Memory spike:** Reading large decimated tile into NumPy array with no bounds
5. **Timeout:** Slow rasters could timeout at 1024×1024 decimation

**Issues in current defaults:**
- `max_pixels = 512 * 512` = 262,144 pixels — could be OK for a large COG
- But `max_values = 256` cap is arbitrary; if raster has 257 unique values in that region, they're silently dropped
- Frontend has no way to know truncation happened (except checking `"truncated": true` flag)

---

### Issue 6: **Colormap RGBA Color Generation Is Fragile**

**Location:** `app/api/v1/endpoints/annotation_sets.py` lines 100–122  
**Severity:** MEDIUM  
**Root Cause:** Hex color parsing doesn't validate, silent fallback to white

**Code:**
```python
def _hex_to_rgba(value: str) -> list[int]:
    raw = (value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)  # Expand shorthand
    if len(raw) == 6:
        raw = f"{raw}ff"  # Add full alpha
    if len(raw) != 8:
        return [255, 255, 255, 255]  # FALLBACK to WHITE
    try:
        return [int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), int(raw[6:8], 16)]
    except ValueError:
        return [255, 255, 255, 255]  # FALLBACK to WHITE
```

**Problems:**
1. **Silent fallback:** Invalid hex silently becomes white—no error raised
2. **No validation:** Style colors could have invalid format; no API-level enforcement
3. **Alpha always full:** Hex `#RRGGBB` gets `FF` appended—no transparency support
4. **Inconsistent format:** Could accept `rgb(r,g,b)` or `hsl()` or other CSS formats, but doesn't

---

### Issue 7: **Value→Class Map Type Coercion Is Lossy**

**Location:** `app/api/v1/endpoints/annotation_sets.py` lines 125–134  
**Severity:** MEDIUM  
**Root Cause:** String keys converted to numeric, losing float precision

**Code:**
```python
def _coerce_value_map(raw_map: dict[str, UUID]) -> dict[str, UUID]:
    coerced: dict[str, UUID] = {}
    for raw_key, cls_id in raw_map.items():
        key = str(raw_key).strip()
        try:
            numeric = int(float(key))  # ← Float then int (loses decimals)
        except ValueError as exc:
            raise HTTPException(...)
        coerced[str(numeric)] = cls_id
    return coerced
```

**Problems:**
1. Raster pixel values could be floats (floating-point GeoTIFFs)
2. `int(float("3.7"))` → `3`, losing the `.7` part
3. Mapping for pixel value `3.7` is silently dropped → unmapped pixels
4. Frontend sends `"3.7": <class_id>`, API converts to `"3": <class_id>`
5. **Result:** Float-valued pixels never match any class

---

### Issue 8: **No Validation That All Raster Values Are Mapped**

**Location:** `app/api/v1/endpoints/annotation_sets.py` lines 567–647  
**Severity:** MEDIUM  
**Root Cause:** No completeness check after colormap build

**Gap:**
```python
# Line 591: Get unique values preview
item = await _get_dataset_item_for_org(db, payload.dataset_item_id, org_id)
value_class_map = _coerce_value_map(payload.value_class_map)
colormap = await _build_colormap(...)  # ← Returns colormap even if incomplete

# ← Missing: Check if value_class_map covers all values from extract_unique_values
```

**Problem:**
- User says "map pixel 1→class A, 2→class B"
- But preview showed values `[0, 1, 2, 5, 7]` — missing 5, 7
- Unmapped values (5, 7) get default color `[0, 0, 0, 0]` (transparent black)
- Frontend has no warning that some pixels won't render

---

### Issue 9: **Nodata Value Coercion Mismatch**

**Location:** `app/api/v1/endpoints/annotation_sets.py` lines 200–202  
**Severity:** MEDIUM  
**Root Cause:** Inconsistent handling of nodata vs. value_class_map

**Code:**
```python
if nodata_value is not None:
    nodata_key = str(int(nodata_value)) if float(nodata_value).is_integer() else str(nodata_value)
    colormap[nodata_key] = [0, 0, 0, 0]
```

**Problem:**
- `nodata_value` is handled differently than `value_class_map` keys
- If nodata is float (e.g., `3.5`), it stays as `"3.5"` in colormap
- But value_class_map keys are all converted to int
- **Inconsistency:** Colormap keys: `{"1": rgba, "3.5": rgba}` (mixed types)
- TiTiler expects all keys to be string representations of same type

---

### Issue 10: **No Test Coverage for Segmentation Mask Mapping**

**Location:** Tests missing for annotation_sets.py raster functionality  
**Severity:** HIGH  
**Root Cause:** No unit/integration tests for the raster config endpoints

**Missing tests:**
- `test_preview_raster_mask_values` — unique value extraction
- `test_configure_raster_mask_for_annotation_set` — colormap building
- `test_raster_mask_colormap_invalid_hex` — color parsing edge cases
- `test_raster_mask_unmapped_values` — incomplete mappings
- `test_raster_mask_float_pixel_values` — float precision loss
- `test_raster_mask_org_isolation` — RLS enforcement on raster tiles

**Impact:** Bugs slip through to production; no regression detection.

---

### Issue 11: **Dataset Item S3 URI Access Not Validated**

**Location:** `app/api/v1/endpoints/annotation_sets.py` line 549  
**Severity:** HIGH  
**Root Cause:** Direct S3 read without checking org ownership

**Code:**
```python
item = await _get_dataset_item_for_org(db, dataset_item_id, org_id)
try:
    preview = await asyncio.to_thread(
        extract_unique_values,
        item.s3_uri,  # ← Direct use of S3 URI
        _gdal_env_for_api(),
        ...
    )
```

**Problem:**
- `_get_dataset_item_for_org()` validates org membership (line 139–154)
- **BUT** `extract_unique_values()` directly opens the S3 file
- If GDAL env is misconfigured, error message could leak S3 paths to logs
- No timeout on S3 read; large files could hang the endpoint

---

### Issue 3B: **Martin Config References Dead Endpoints** ⚠️ **TO REMOVE**

**Location:** `infra/docker/martin/config.yaml`  
**Severity:** LOW  
**Status:** Not used

**Problem:**
- `infra/docker/martin/config.yaml` defines `annotation_set_mvt` function source
- But `/annotation-sets/{set_id}/tiles/{z}/{x}/{y}.pbf` endpoint (Issue #3) is never called
- Martin container is running but not serving any segmentation mask tiles

**Action:** Remove Martin from docker-compose.yml or repurpose for other uses (future vector layers).

---

### Issue 13: **Raster Mask Config Update Modifies Map Layer Incorrectly**

**Location:** `app/api/v1/endpoints/annotation_sets.py` lines 602–618  
**Severity:** MEDIUM  
**Root Cause:** Side-effect on map_layer during raster config save

**Code:**
```python
map_layer_id = payload.map_layer_id
if map_layer_id is not None:
    layer = await _get_map_layer_for_org(db, map_layer_id, set_id, org_id)
    source_config = dict(layer.source_config or {})
    source_config["raster_mask"] = {…}
    layer.source_config = source_config
    layer.layer_type = "raster"  # ← Changes layer type!
```

**Problems:**
1. **Overwrites layer_type:** If layer was previously `"annotation"` (vector), becomes `"raster"`
2. **Destructive:** Cannot have mixed vector + raster in same layer
3. **No rollback:** If config update fails mid-transaction, layer is left in inconsistent state
4. **No audit:** Changing layer_type doesn't emit audit event (only raster_config.update does)

---

### Issue 14: **TiTiler Auto-Rescale Disabled for Raster Masks**

**Location:** `app/api/v1/endpoints/annotation_sets.py` line 632  
**Severity:** MEDIUM  
**Root Cause:** Tile URL template doesn't include rendering params

**Current behavior:**
```python
tile_url_template = (
    f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1/tiles/collections/"
    f"{item.stac_collection_id}/items/{item.stac_item_id}/{{z}}/{{x}}/{{y}}.png"
)
```

**Problem:**
- Generic item tile endpoint is called
- No `colormap=` or `rescale=` params in template
- Frontend must append colormap **as URL query param**
- **But** `/tiles/...` proxy (tiles.py) doesn't handle colormap params—passes them to TiTiler verbatim
- If frontend forgets colormap, user sees raw pixel values (not helpful for classification)

---

### Issue 15: **No Handling for Multi-Band Rasters in Segmentation Mode**

**Location:** `app/workers/ingestion/rasterio_utils.py`, `app/api/v1/endpoints/annotation_sets.py`  
**Severity:** MEDIUM  
**Root Cause:** Assumes single-band input for segmentation masks

**Problem:**
- Segmentation mask config specifies `band_index` (lines 48, 612)
- But `extract_unique_values()` always reads **one band only**
- If a user uploads a multi-band raster expecting band-3 to be the mask:
  1. Preview extracts values from band 1 (default)
  2. User maps pixel values assuming band 1
  3. Config saves `band_index=1` instead of requested band
  4. Rendered tiles show wrong data

**Root cause:** No UI flow to specify which band to use for preview.

---

## III. Data Flow Issues

### Issue 16: **AnnotationSet → Dataset → RasterMask Connection Is Loose**

**Model structure** (annotation_set.py lines 27–58):
```python
class AnnotationSet(Base):
    dataset_id: UUID | None  # Optional FK
    stac_item_id: str | None
    # No "raster_mask_config" column
    # No "is_segmentation_mask" flag
```

**Problem:**
- `annotation_set.dataset_id` and `stac_item_id` are optional
- No way to distinguish "this is a raster mask set" vs. "this is a vector annotation set"
- Raster config is stored in **associated map_layer.source_config**, not in annotation_set itself
- **Result:** Cannot query "all raster mask sets" without joining through map→layers

**Impact:** Listing/filtering raster masks requires indirect queries; no type safety.

---

### Issue 17: **Colormap Not Persisted in AnnotationSet**

**Location:** `app/models/annotation_set.py`  
**Severity:** MEDIUM  
**Root Cause:** Colormap is computed on-the-fly, not stored

**Current flow:**
1. User saves raster config → `_build_colormap()` (line 592)
2. Colormap returned to frontend (line 645)
3. **NOT SAVED** to any table
4. If frontend wants colormap later → must recompute from class styles

**Problem:**
- Class styles can change → colormap changes
- If class style was `#FF0000`, later changed to `#00FF00`, colormap silently changes
- Old tiles rendered with `#FF0000` pixels are now `#00FF00`
- No versioning or consistency guarantee

**Better design:** Store computed colormap in `source_config["colormap"]` so it's immutable.

---

## IV. Frontend Integration Issues

### Issue 18: **Colormap URL Parameter Can Exceed URL Length Limits**

**Documentation** (raster_segmentation_mask.md):
```typescript
const colormapQ = encodeURIComponent(JSON.stringify(config.colormap));
const url = `${config.tile_url_template}?asset_bidx=data|${config.band_index}&colormap=${colormapQ}`;
```

**Problem:**
- For a 256-class segmentation mask, colormap is ~8KB JSON
- URL-encoded: ~12KB
- Tile URL becomes: `/tiles/.../tiles/{z}/{x}/{y}.png?colormap=<12KB>`
- **Issue:** Many proxy/CDN solutions reject URLs > 2KB–8KB
- **Fallback:** Nginx default max `4KB`, some CDNs `8KB`

**Impact:** Large colormaps fail silently; users see blank tiles.

---

## V. Performance & Scalability Issues

### Issue 19: **No Tile Caching Strategy for Raster Masks**

**Location:** `app/services/titiler_service.py`  
**Severity:** MEDIUM  
**Root Cause:** Colormap in URL query string bypasses HTTP caching

**Problem:**
- TiTiler respects HTTP `Cache-Control` headers
- But each tile request has different `colormap=` query param
- Browser/CDN sees each request as unique → **no cache hit**
- Result: Every zoom level, every pan request hits TiTiler
- For a user panning a 1000×1000 px viewport, ~1000 tile requests, **0 cache hits**

**Better approach:** Store colormap on server, reference by ID in URL.

---

### Issue 20: **Unique Value Extraction Blocks Tile Requests**

**Location:** `app/api/v1/endpoints/annotation_sets.py` line 546  
**Severity:** MEDIUM  
**Root Cause:** Synchronous rasterio operation in async endpoint

**Code:**
```python
preview = await asyncio.to_thread(
    extract_unique_values,  # Blocking I/O
    item.s3_uri,
    ...
)
```

**Problem:**
- `asyncio.to_thread()` offloads to thread pool, but:
  1. Default thread pool size is ~5 threads
  2. If multiple users request previews simultaneously, threads exhaust
  3. Subsequent requests queue, timeout after 30 seconds
- No priority queue; high-value user's tile request waits behind preview request

**Scenario:** User drags on map (tile requests) while another user clicks "preview values" → tiles stall.

---

### Issue 21: **CRITICAL: No Frontend UI for Segmentation Mask Visualization**

**Location:** Frontend (map layer component)  
**Severity:** CRITICAL  
**Root Cause:** Frontend has no UI to select/toggle segmentation masks on map

**Missing functionality:**
1. **List segmentation masks** for current project/dataset
   - Show mask name, class count, last updated
   - Filter by dataset or date range
2. **Add mask to map**
   - Click to add as overlay layer
   - Auto-assign unique name (Mask 1, Mask 2, etc.)
3. **Layer control**
   - Toggle visibility on/off
   - Adjust opacity (0–100%)
   - Reorder layers (bring to front/back)
4. **Legend display**
   - Show all classes in mask
   - Class ID, class name, class color
   - Count of pixels per class (optional)
5. **Interact with mask**
   - Click pixel to see class info
   - Hover to highlight class
   - Filter by class (hide other classes)

**Current state:**
- Raster config endpoint returns colormap → frontend ignores it
- No dropdown to select which raster to view
- Impossible to overlay multiple segmentation masks
- User cannot see which classes are in the mask

**Impact:** Feature is completely invisible to users; they have no way to access it even if backend works.

---

## VI. Summary of Critical Blockers

| ID | Issue | Severity | Blocker? | Resolution |
|---|---|---|---|---|
| 1 | Raster mask config ignored by tile endpoint | CRITICAL | YES | Rewrite tile endpoint to apply stored colormap |
| 2 | Colormap not applied to tile requests | CRITICAL | YES | Pass colormap to TiTiler at request time |
| 3 | Martin MVT endpoints are dead code | LOW | NO | **DELETE** — Leaflet doesn't support PBF format |
| 3B | Martin config not needed | LOW | NO | Remove from docker-compose.yml |
| 4 | No RLS enforcement on tile access | CRITICAL | YES | Add `org_id` check to `/tiles/` endpoints |
| 5 | Pixel extraction not production-safe | HIGH | YES | Add bounds, timeout, error handling to `extract_unique_values` |
| 6 | Color parsing fragile | MEDIUM | No | Validate hex format, add opacity support |
| 7 | Value map loses float precision | MEDIUM | No | Keep float keys in colormap |
| 8 | No validation of complete mapping | MEDIUM | No | Warn if preview values not fully mapped |
| 9 | Nodata coercion mismatch | MEDIUM | No | Consistent int/float handling in colormap |
| 10 | No test coverage | HIGH | YES | Add comprehensive test suite |
| 11 | S3 URI access not validated | HIGH | YES | Verify org ownership before S3 read |
| 13 | Map layer overwrite on raster config | MEDIUM | No | Don't auto-change `layer_type` on update |
| 14 | TiTiler rescale not auto-applied | MEDIUM | No | Include rescale in colormap params |
| 15 | Multi-band handling incomplete | MEDIUM | No | Allow UI to select band for preview |
| 16 | Loose raster↔dataset connection | MEDIUM | Design | Add `is_segmentation_mask` flag to AnnotationSet |
| 17 | Colormap not persisted | MEDIUM | Design | Store colormap immutably in DB for auditability |
| 18 | Colormap URL exceeds length limit | MEDIUM | No | Use server-side colormap ID, not URL-encoded param |
| 19 | No tile caching for raster | MEDIUM | Performance | Use colormap hash in URL for cache busting |
| 20 | Unique value extraction blocks tiles | MEDIUM | Performance | Move to background worker, add priority queue |
| **21** | **No frontend UI for segmentation masks** | **CRITICAL** | **YES** | **Build layer control UI + legend + opacity/filters** |

---

## VII. Recommended Action Plan

### **CRITICAL PATH (Must Complete for Feature to Work)**

#### Phase 1: Fix Tile Rendering (Issues 1, 2, 4, 21)
1. **Rewrite tile endpoint** to use stored raster config
   - Load `annotation_set` or `map_layer.source_config` by `dataset_item_id`
   - Build colormap from `value_class_map` + class styles
   - Pass colormap to TiTiler with `colormap=` query param
   - Add RLS check: `WHERE organization_id = current_org_id`

2. **Add RLS enforcement** to all tile endpoints
   - Verify org owns the dataset before serving tiles
   - Return 403 if cross-org access attempted

3. **Frontend: Build segmentation mask layer UI**
   - Dropdown to list all segmentation masks in project
   - Add to map button → creates new layer
   - Layer control panel:
     - Toggle visibility
     - Adjust opacity (0–100%)
     - Show legend (class_id → class_name → class_color)
     - Remove layer button
   - Click pixel to show class info (optional)

4. **Add test coverage** (Issues 10, 11)
   - Test raster config save + colormap build
   - Test tile rendering with colormap
   - Test RLS enforcement
   - Test S3 access validation

**Timeline:** ~2–3 weeks (backend 1 week, frontend 1–2 weeks, tests + integration 1 week)

---

#### Phase 2: Fix Data Integrity (Issues 5, 7–9, 15–17)
1. Robust pixel value extraction
   - Add timeout, memory bounds
   - Better error messages
   - Handle float-valued pixels (keep as string keys in colormap)

2. Add schema changes
   - Add `is_segmentation_mask: bool` flag to AnnotationSet
   - Persist colormap in `map_layer.source_config` or new `raster_config` table

3. Validate completeness
   - Warn if preview values not fully mapped
   - Add validation in `_coerce_value_map()` and `_build_colormap()`

**Timeline:** ~1 week

---

#### Phase 3: Cleanup & Performance (Issues 3, 3B, 18–20)
1. **Remove dead code**
   - Delete Martin proxy endpoints (lines 763–820 in annotation_sets.py)
   - Remove Martin from docker-compose.yml (unless needed for other features)
   - Remove `_martin_client` setup

2. **Optimize tile caching**
   - Use colormap hash in URL for cache busting
   - Move unique value extraction to background worker

**Timeline:** ~3–5 days

---

## VIII. Implementation Details

### Backend: New Tile Endpoint for Segmentation Masks

Instead of generic `/tiles/collections/{cid}/items/{iid}/{z}/{x}/{y}.png`, create:

```python
@router.get("/raster-masks/{annotation_set_id}/{z}/{x}/{y}.{fmt}")
async def proxy_raster_mask_tile(
    annotation_set_id: UUID,
    z: int, x: int, y: int, fmt: str,
    request: Request,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
) -> Response:
    # Load annotation set + validate org
    ann_set = await db.get(AnnotationSet, annotation_set_id)
    if ann_set is None or ann_set.organization_id != org_id:
        raise HTTPException(status_code=404)
    
    # Load raster config from map_layer or direct from annotation_set
    # Build colormap from value_class_map + class styles
    colormap = await _build_colormap_for_annotation_set(db, ann_set)
    
    # Pass to TiTiler with colormap
    stac_item_id = ann_set.stac_item_id
    titiler_path = f"/collections/{ann_set.stac_collection_id}/items/{stac_item_id}/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    params = {"colormap": json.dumps(colormap)}
    return await _proxy_tile(titiler_path, params)
```

### Frontend: Segmentation Mask Layer Component

```typescript
// In map-layer.tsx or new segmentation-mask-layer.tsx
const SegmentationMaskLayer = ({ annotationSetId, mapInstance }) => {
  const [opacity, setOpacity] = useState(0.7);
  const [visible, setVisible] = useState(true);
  const { data: config } = useQuery(`/api/v1/annotation-sets/${annotationSetId}/raster/config`);
  
  const tileUrl = `/api/v1/raster-masks/${annotationSetId}/{z}/{x}/{y}.png`;
  
  useEffect(() => {
    const layer = L.tileLayer(tileUrl, { opacity, visible });
    mapInstance.addLayer(layer);
  }, [annotationSetId]);
  
  return (
    <LayerControl>
      <Opacity value={opacity} onChange={setOpacity} />
      <Legend classIds={config.value_class_map} />
      <RemoveButton />
    </LayerControl>
  );
};
```

---

## IX. Verification Checklist

- [ ] Tile endpoint reads raster config from DB
- [ ] Colormap is passed to TiTiler and applied to tiles
- [ ] RLS check prevents cross-org access
- [ ] Frontend dropdown lists all segmentation masks
- [ ] Clicking "Add to map" creates new layer
- [ ] Layer shows legend with class colors
- [ ] Opacity slider works
- [ ] Layer toggle on/off works
- [ ] Tests pass (rendering, RLS, data integrity)
- [ ] Martin dead code removed
- [ ] Documentation updated with UI screenshots

