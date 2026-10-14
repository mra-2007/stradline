# Strandline HTTP API Contract

Base URL: `http://localhost:8000`

All endpoints return JSON by default (except `/runs/{run_id}/sitrep`, which returns a PDF).

---

## Error Format

Every error returns HTTP status $\ge 400$ with uniform JSON:

```json
{
  "error": "Descriptive error message",
  "hint": "Actionable guidance or hint"
}
```

---

## Endpoints

### 1. Health Check
- **Method:** `GET`
- **Path:** `/health`
- **Request Example:**
  ```http
  GET /health HTTP/1.1
  Host: localhost:8000
  ```
- **Response Example (200 OK):**
  ```json
  {
    "status": "ok"
  }
  ```

---

### 2. Track Preflight
Queries Sentinel-1 metadata catalogue to find usable same-orbit acquisitions without downloading rasters.
- **Method:** `GET`
- **Path:** `/preflight?bbox={W,S,E,N}&date={YYYY-MM-DD}`
- **Request Example:**
  ```http
  GET /preflight?bbox=85.10,27.85,85.45,28.30&date=2026-08-26 HTTP/1.1
  Host: localhost:8000
  ```
- **Response Example (200 OK - Track Found):**
  ```json
  {
    "chosen": {
      "track": ["orbit", 19, "DESCENDING"],
      "post": "2026-08-28",
      "latency_days": 2,
      "pre": ["2026-08-16", "2026-08-04", "2026-07-23"],
      "notes": []
    },
    "message": "usable track found"
  }
  ```
- **Response Example (200 OK - No Usable Track):**
  ```json
  {
    "chosen": null,
    "message": "no usable track"
  }
  ```
- **Error Example (400 Bad Request):**
  ```json
  {
    "error": "bbox must be 4 comma-separated floats: W,S,E,N",
    "hint": "Pass ?bbox=W,S,E,N (e.g. ?bbox=85.10,27.85,85.45,28.30)"
  }
  ```

---

### 3. Start or Find Run
Launches the full pipeline as a background thread. If an identical run (`bbox` and `event_date`) has already completed, returns the existing `run_id`.
- **Method:** `POST`
- **Path:** `/runs`
- **Request Example:**
  ```http
  POST /runs HTTP/1.1
  Host: localhost:8000
  Content-Type: application/json

  {
    "bbox": [85.10, 27.85, 85.45, 28.30],
    "event_date": "2026-08-26"
  }
  ```
- **Response Example (200 OK):**
  ```json
  {
    "run_id": "api_4f1c9d2a"
  }
  ```

---

### 4. Run Status
Returns stage-by-stage progress and state (`queued`, `running`, `done`, `refused`, `failed`).
- **Method:** `GET`
- **Path:** `/runs/{run_id}/status`
- **Request Example:**
  ```http
  GET /runs/case_smoke/status HTTP/1.1
  Host: localhost:8000
  ```
- **Response Example (200 OK):**
  ```json
  {
    "state": "done",
    "stages": ["preflight", "fetch", "prepare", "classify", "write"],
    "error": null
  }
  ```
- **Response Example (200 OK - Refused):**
  ```json
  {
    "state": "refused",
    "stages": ["preflight"],
    "error": "no usable same-track Sentinel-1 pair for this area and date"
  }
  ```
- **Error Example (404 Not Found):**
  ```json
  {
    "error": "Run not found",
    "hint": "No run found with id 'unknown_run'"
  }
  ```

---

### 5. Run Provenance Report
Returns the full `run_report.json` document.
- **Method:** `GET`
- **Path:** `/runs/{run_id}/report`
- **Request Example:**
  ```http
  GET /runs/case_smoke/report HTTP/1.1
  Host: localhost:8000
  ```
- **Response Example (200 OK):**
  ```json
  {
    "event_date": "2026-08-26",
    "bbox_wgs84": [85.1, 27.85, 85.45, 28.3],
    "areas_ha": {
      "water_high": 142.3,
      "water_medium": 89.1,
      "debris": 34.0,
      "permanent_water": 412.5
    },
    "runtime_s": 45.2
  }
  ```

---

### 6. Vector GeoJSON Layers
Fetches GeoJSON feature collections for specific pipeline output layers.
- **Method:** `GET`
- **Path:** `/runs/{run_id}/layers/{name}`
- **Layer Name Mapping:**
  - `flood` $\rightarrow$ `flood_polygons.geojson`
  - `low_confidence` $\rightarrow$ `reliability_narrow_valley.geojson`
  - `damaged_buildings` $\rightarrow$ `damaged_buildings.geojson`
  - `cut_roads` $\rightarrow$ `cut_roads.geojson`
  - `bridges_out` $\rightarrow$ `bridges_out.geojson`
  - `settlements` $\rightarrow$ `settlements.geojson`
  - `routes` $\rightarrow$ `routes.geojson`
- **Request Example:**
  ```http
  GET /runs/case_smoke/layers/flood HTTP/1.1
  Host: localhost:8000
  ```
- **Response Example (200 OK):**
  ```json
  {
    "type": "FeatureCollection",
    "features": [
      {
        "type": "Feature",
        "geometry": { "type": "Polygon", "coordinates": [...] },
        "properties": { "class": 2, "class_name": "flood_high", "area_ha": 3.4 }
      }
    ]
  }
  ```
- **Error Example (404 Not Found):**
  ```json
  {
    "error": "Unknown layer 'invalid'",
    "hint": "Available layers: flood, low_confidence, damaged_buildings, cut_roads, bridges_out, settlements, routes"
  }
  ```

---

### 7. Situation-Report Copilot (Ask)
Queries the verified grounding copilot about a specific run in English (`en`) or Nepali (`ne`).
- **Method:** `POST`
- **Path:** `/runs/{run_id}/ask`
- **Request Example:**
  ```http
  POST /runs/case_smoke/ask HTTP/1.1
  Host: localhost:8000
  Content-Type: application/json

  {
    "question": "how many buildings are damaged?",
    "lang": "en"
  }
  ```
- **Response Example (200 OK):**
  ```json
  {
    "text": "24 of 120 buildings (20%) are damaged.",
    "numbers": [
      {
        "value": 24,
        "unit": "count",
        "fact_id": "damage.buildings_damaged",
        "formatted": "24",
        "feature_ids": ["way/100", "way/101"],
        "source_file": "damaged_buildings.geojson",
        "derivation": "count"
      }
    ],
    "trace": [...],
    "refused": false,
    "reason": null,
    "lang": "en"
  }
  ```

---

### 8. Situation Report PDF (Sitrep)
Retrieves or builds the one-page situation report PDF in English or Nepali.
- **Method:** `GET`
- **Path:** `/runs/{run_id}/sitrep?lang={en|ne}`
- **Request Example:**
  ```http
  GET /runs/case_smoke/sitrep?lang=en HTTP/1.1
  Host: localhost:8000
  ```
- **Response Example (200 OK):**
  Binary stream (`Content-Type: application/pdf`, `Content-Disposition: inline; filename="sitrep_en.pdf"`).

---

### 9. List Runs
Every run this server knows about, newest first. Includes `case_smoke`, any run started through
`POST /runs`, and any directory under `runs/` (one or two levels deep) that holds a
`run_report.json`. The dashboard's run switcher reads this.
- **Method:** `GET`
- **Path:** `/runs`
- **Response Example (200 OK):**
  ```json
  {
    "runs": [
      {
        "run_id": "case_smoke",
        "bbox": [85.1, 27.85, 85.45, 28.3],
        "event_date": "2026-08-26",
        "state": "done",
        "created_at": "2026-10-13T11:05:02.117412+00:00"
      }
    ]
  }
  ```

---

### 10. Terrain Tiles
Terrarium-encoded elevation tiles cut from the run's own cached Copernicus DEM
(`runs/{run_id}/cache/dem_20m.tif`), for MapLibre's `raster-dem` source. No network is touched at
request time. A tile outside the DEM's footprint is returned flat (0 m everywhere, RGB `128,0,0`)
rather than as an error, so the 3D mesh does not tear at the edge of the mapped area.
Credit **"Copernicus DEM"** wherever these tiles are drawn.
- **Method:** `GET`
- **Path:** `/runs/{run_id}/terrain/{z}/{x}/{y}.png`
- **Encoding:** `elevation_m = R * 256 + G + B / 256 - 32768`
- **Request Example:**
  ```http
  GET /runs/case_smoke/terrain/11/1509/857.png HTTP/1.1
  Host: localhost:8000
  ```
- **Response (200 OK):** `image/png`, 256x256, header `X-Attribution: Copernicus DEM`,
  `Cache-Control: public, max-age=86400`.
- **Error Example (404 Not Found):**
  ```json
  {
    "error": "No cached Copernicus DEM for this run",
    "hint": "The run may still be in progress; the DEM is written during the terrain stage"
  }
  ```

---

### 11. Radar Quicklook
The run's `quicklook.png` as produced by the pipeline: the radar evidence behind the map.
- **Method:** `GET`
- **Path:** `/runs/{run_id}/quicklook`
- **Request Example:**
  ```http
  GET /runs/case_smoke/quicklook HTTP/1.1
  Host: localhost:8000
  ```
- **Response (200 OK):** `image/png`.
- **Error Example (404 Not Found):**
  ```json
  {
    "error": "quicklook.png not found for this run",
    "hint": "The quicklook is written at the end of a successful run"
  }
  ```
