"""Strandline HTTP API powered by FastAPI.
"""

from __future__ import annotations

import json
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date as dt_date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api import store, terrain
from copilot.executor import answer as copilot_answer
from copilot import sitrep as copilot_sitrep
from strandline.config import (
    DamageParams,
    DetectParams,
    MonteCarloParams,
    NetworkParams,
    OpticalParams,
    OsmParams,
    RunParams,
)
from strandline.run import preflight as strandline_preflight
from strandline.run import run as strandline_run

ROOT = Path(__file__).resolve().parents[1]

LAYER_FILE_MAP: dict[str, str] = {
    "flood": "flood_polygons.geojson",
    "low_confidence": "reliability_narrow_valley.geojson",
    "damaged_buildings": "damaged_buildings.geojson",
    "cut_roads": "cut_roads.geojson",
    "bridges_out": "bridges_out.geojson",
    "settlements": "settlements.geojson",
    "routes": "routes.geojson",
}


class RunCreateRequest(BaseModel):
    bbox: list[float] = Field(..., min_length=4, max_length=4, description="[W, S, E, N] coordinates")
    event_date: str = Field(..., description="Event date in YYYY-MM-DD format")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Question in English or Nepali")
    lang: str = Field("en", description="Language: 'en' or 'ne'")
    offline: bool = Field(
        False,
        description="Skip the language model and answer from the offline templates only. "
        "Used by the dashboard when the model-backed call has already timed out.",
    )


def _run_pipeline_worker(run_id: str, bbox: tuple[float, float, float, float],
                         event_day: dt_date, out_dir: Path) -> None:
    try:
        store.update_run(run_id, state="running", stage="preflight")
        rp = RunParams()
        dp = DetectParams()
        op = OpticalParams()
        osmp = OsmParams()
        dmgp = DamageParams()
        npar = NetworkParams()
        mc = MonteCarloParams()

        exit_code = strandline_run(
            bbox, event_day, out_dir, rp, dp, dry_run=False,
            op=op, osmp=osmp, dmgp=dmgp, npar=npar, mc=mc
        )
        if exit_code == 0:
            store.update_run(run_id, state="done", stage="completed")
        elif exit_code == 2:
            store.update_run(
                run_id,
                state="refused",
                error="no usable same-track Sentinel-1 pair for this area and date"
            )
        else:
            store.update_run(run_id, state="failed", error=f"Pipeline exited with code {exit_code}")
    except Exception as err:
        store.update_run(run_id, state="failed", error=str(err))


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    store.register_case_smoke(base_dir=ROOT)
    store.discover_runs(base_dir=ROOT)
    store.prune_missing_runs()
    yield


app = FastAPI(title="Strandline API", lifespan=lifespan)

# Allow CORS from frontend dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    hint = "Check your request parameters"
    if exc.headers and "X-Hint" in exc.headers:
        hint = exc.headers["X-Hint"]
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"error": detail, "hint": hint})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    msg = errors[0].get("msg", "Validation error") if errors else "Invalid request payload"
    loc = " -> ".join(str(part) for part in errors[0].get("loc", [])) if errors else ""
    error_str = f"{msg} ({loc})" if loc else msg
    return JSONResponse(
        status_code=422,
        content={"error": error_str, "hint": "Ensure payload conforms to required types and formats"}
    )


@app.exception_handler(Exception)
async def generic_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "hint": "An internal error occurred"}
    )


# --- API Routes ---------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/preflight")
def preflight(
    bbox: str = Query(..., description="Bounding box as W,S,E,N"),
    date: str = Query(..., description="Event date as YYYY-MM-DD"),
) -> dict[str, Any]:
    try:
        parts = [float(x.strip()) for x in bbox.split(",")]
        if len(parts) != 4:
            raise ValueError
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="bbox must be 4 comma-separated floats: W,S,E,N",
            headers={"X-Hint": "Pass ?bbox=W,S,E,N (e.g. ?bbox=85.10,27.85,85.45,28.30)"}
        )

    try:
        event_day = dt_date.fromisoformat(date)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date '{date}'",
            headers={"X-Hint": "Pass date in YYYY-MM-DD format"}
        )

    rp = RunParams()
    try:
        choice = strandline_preflight(tuple(parts), event_day, rp)
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"Catalogue preflight failed: {err}")

    if choice is None:
        return {"chosen": None, "message": "no usable track"}

    return {
        "chosen": {
            "track": list(choice.track),
            "post": str(choice.post.day),
            "latency_days": choice.latency_days,
            "pre": [str(a.day) for a in choice.pre],
            "notes": list(choice.notes),
        },
        "message": "usable track found"
    }


@app.post("/runs")
def create_run(req: RunCreateRequest) -> dict[str, str]:
    try:
        event_day = dt_date.fromisoformat(req.event_date)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid event_date '{req.event_date}'",
            headers={"X-Hint": "Pass event_date in YYYY-MM-DD format"}
        )

    finished = store.find_finished_run(req.bbox, req.event_date)
    if finished is not None:
        return {"run_id": finished["run_id"]}

    run_id = uuid.uuid4().hex[:8]
    out_dir = ROOT / "runs" / f"api_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    store.create_run(run_id, req.bbox, req.event_date, str(out_dir))

    thread = threading.Thread(
        target=_run_pipeline_worker,
        args=(run_id, tuple(req.bbox), event_day, out_dir),
        daemon=True,
    )
    thread.start()

    return {"run_id": run_id}


@app.get("/runs")
def list_runs() -> dict[str, Any]:
    """Every run this server knows about, newest first.

    The dashboard's run switcher reads this. It is a listing of directories
    under `runs/` that hold a `run_report.json`, plus anything started through
    `POST /runs` in this process.
    """
    items = store.list_runs()
    return {
        "runs": [
            {
                "run_id": item["run_id"],
                "bbox": item["bbox"],
                "event_date": item["event_date"],
                "state": item["state"],
                "created_at": item["created_at"],
            }
            for item in items
        ]
    }


@app.get("/runs/{run_id}/status")
def run_status(run_id: str) -> dict[str, Any]:
    item = store.get_run(run_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="Run not found",
            headers={"X-Hint": f"No run found with id '{run_id}'"}
        )
    return {
        "state": item["state"],
        "stages": item["stages"],
        "error": item["error"]
    }


@app.get("/runs/{run_id}/report")
def run_report(run_id: str) -> dict[str, Any]:
    item = store.get_run(run_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="Run not found",
            headers={"X-Hint": f"No run found with id '{run_id}'"}
        )

    out_dir = Path(item["out_dir"])
    report_path = out_dir / "run_report.json"
    if not report_path.exists():
        raise HTTPException(
            status_code=404,
            detail="run_report.json not found",
            headers={"X-Hint": "The run may still be in progress, failed or refused"}
        )

    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as err:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read run_report.json: {err}",
            headers={"X-Hint": "Check file permissions and integrity"}
        )


@app.get("/runs/{run_id}/layers/{name}")
def run_layer(run_id: str, name: str) -> dict[str, Any]:
    filename = LAYER_FILE_MAP.get(name)
    if not filename:
        valid_layers = ", ".join(LAYER_FILE_MAP.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Unknown layer '{name}'",
            headers={"X-Hint": f"Available layers: {valid_layers}"}
        )

    item = store.get_run(run_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="Run not found",
            headers={"X-Hint": f"No run found with id '{run_id}'"}
        )

    layer_path = Path(item["out_dir"]) / filename
    if not layer_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Layer '{name}' not found on disk",
            headers={"X-Hint": f"Expected file '{filename}' has not been generated for this run"}
        )

    try:
        return json.loads(layer_path.read_text(encoding="utf-8"))
    except Exception as err:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read layer '{name}': {err}",
            headers={"X-Hint": "Check file contents and permissions"}
        )


@app.get("/runs/{run_id}/terrain/{z}/{x}/{y}.png")
def run_terrain_tile(run_id: str, z: int, x: int, y: int) -> Response:
    """A terrarium-encoded elevation tile from this run's own Copernicus DEM.

    Outside the DEM's footprint the tile is flat (0 m) rather than an error, so
    the 3D mesh does not tear at the edge of the mapped area.
    """
    item = store.get_run(run_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="Run not found",
            headers={"X-Hint": f"No run found with id '{run_id}'"}
        )

    dem_path = terrain.find_dem(Path(item["out_dir"]))
    if dem_path is None:
        raise HTTPException(
            status_code=404,
            detail="No cached Copernicus DEM for this run",
            headers={"X-Hint": "The run may still be in progress; the DEM is written during the terrain stage"}
        )

    try:
        png = terrain.terrarium_tile_png(dem_path, z, x, y)
    except Exception as err:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read terrain tile: {err}",
            headers={"X-Hint": "Check that the run's cached DEM is a readable GeoTIFF"}
        )

    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Attribution": terrain.TERRAIN_ATTRIBUTION,
        },
    )


@app.get("/runs/{run_id}/quicklook")
def run_quicklook(run_id: str) -> FileResponse:
    """The run's radar quicklook PNG, as produced by the pipeline."""
    item = store.get_run(run_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="Run not found",
            headers={"X-Hint": f"No run found with id '{run_id}'"}
        )

    path = Path(item["out_dir"]) / "quicklook.png"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="quicklook.png not found for this run",
            headers={"X-Hint": "The quicklook is written at the end of a successful run"}
        )

    return FileResponse(path=str(path), media_type="image/png", filename="quicklook.png")


@app.post("/runs/{run_id}/ask")
def ask_copilot(run_id: str, req: AskRequest) -> dict[str, Any]:
    item = store.get_run(run_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="Run not found",
            headers={"X-Hint": f"No run found with id '{run_id}'"}
        )

    run_dir = Path(item["out_dir"])
    if not run_dir.exists():
        raise HTTPException(
            status_code=404,
            detail="Run directory not found on disk",
            headers={"X-Hint": "Run output directory is missing"}
        )

    try:
        answer_dict = copilot_answer(run_dir, req.question, req.lang, use_llm=not req.offline)
        return answer_dict
    except Exception as err:
        raise HTTPException(
            status_code=500,
            detail=f"Copilot failed to answer: {err}",
            headers={"X-Hint": "Ensure run output files are complete"}
        )


@app.get("/runs/{run_id}/sitrep")
def get_sitrep(run_id: str, lang: str = "en") -> FileResponse:
    if lang not in ("en", "ne"):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language '{lang}'",
            headers={"X-Hint": "Allowed lang values are 'en' or 'ne'"}
        )

    item = store.get_run(run_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="Run not found",
            headers={"X-Hint": f"No run found with id '{run_id}'"}
        )

    run_dir = Path(item["out_dir"])
    pdf_path = run_dir / f"sitrep_{lang}.pdf"

    if not pdf_path.exists():
        try:
            copilot_sitrep.build(run_dir, lang=lang, out=pdf_path)
        except Exception as err:
            raise HTTPException(
                status_code=500,
                detail=f"Sitrep generation failed: {err}",
                headers={"X-Hint": "A headless browser (Chrome or Edge) is required for PDF generation"}
            )

    if not pdf_path.exists():
        raise HTTPException(
            status_code=500,
            detail="Sitrep PDF was not created",
            headers={"X-Hint": "Check headless browser setup"}
        )

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"sitrep_{lang}.pdf",
    )


# --- Static Files Mounted LAST -----------------------------------------------
# Mounted after all API routes are registered, so "/" never hides /health, /preflight or /runs.
dist_dir = ROOT / "web" / "dist"
if dist_dir.exists() and dist_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="static")
