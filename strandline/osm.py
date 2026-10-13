"""OpenStreetMap features from the ohsome API, as they stood BEFORE the event.

Rule 1, and the reason this module is shaped the way it is: OSM is an allowed
pipeline input only as a snapshot dated before the event. After a disaster,
mappers trace the damage - flooded buildings get `flood:date`, washed-out
bridges get deleted, humanitarian mapping campaigns redraw whole valleys. A
"current" OSM download of a flood area therefore contains other people's
damage assessment, and using it would be reading the answer. So this module
never asks for today's OSM. It asks for one specific instant:

    the latest date strictly BEFORE the event date, and not after the latest
    snapshot the ohsome instance can serve (OsmParams.latest_snapshot)

That date is returned, cached under, and written into run_report.json, so every
count downstream can be traced to the OSM it was computed from.

WHICH ENDPOINT, AND WHY TWO

ohsome offers a snapshot extraction at `/elements/geometry`, which is the
natural fit. Measured on 2026-10-13 from this network, that path answers
**403 Forbidden** at the WAF while `/metadata`, the aggregation endpoints and
`/elementsFullHistory/geometry` all answer 200. Rather than depend on one path
being unblocked during live judging, both are tried in order and the one that
answered is recorded:

  1. `/elements/geometry` - one version per element, as at `time`.
  2. `/elementsFullHistory/geometry` over a short window ENDING at the snapshot
     instant. This returns every version valid during the window, so it is
     reduced to the snapshot state here: per OSM id, keep the version with the
     latest `@validFrom`, and only where `@validTo` reaches the window end
     (an element deleted mid-window does not reach it, and so is correctly
     absent from the snapshot).

Both paths produce the same thing: one feature per OSM element, with its tags,
as at the snapshot. `snapshot_features` does the reduction and is pure, so the
dedupe is tested without a network.

FAILURE IS A REFUSAL, NOT A GUESS

Every failure - unreachable host, 403, 413 on too large an area, a malformed
body, an empty response where features were required - raises `OsmError` with
what failed, why, and what to try. A damage map built on a silently empty OSM
layer would report zero damaged buildings and look like good news.
"""

from __future__ import annotations

import hashlib
import json
import math
import time as _time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import requests

from strandline.config import OSM_LAYERS, OsmParams

# The earliest instant the ohsome instance can serve (its /metadata
# temporalExtent.fromTimestamp, read 2026-10-13). A snapshot before this cannot
# be answered, so it is refused here with a clear reason instead of as an opaque
# API error.
OHSOME_HISTORY_START = date(2007, 10, 8)

# Window length for the full-history fallback. One day is enough to contain the
# snapshot instant while keeping the response small; the reduction below is
# what actually picks the snapshot state, so this is a transfer-size choice.
FULL_HISTORY_WINDOW_DAYS = 1

# ohsome property names carrying element identity and version validity.
OSM_ID = "@osmId"
VALID_FROM = "@validFrom"
VALID_TO = "@validTo"


class OsmError(RuntimeError):
    """OSM could not be read for this area and date. Says what to try."""


# --- what to ask for ----------------------------------------------------------

def snapshot_date(event_day: date, params: OsmParams | None = None) -> date:
    """The OSM instant to read: latest date strictly before the event, capped.

    Strictly before, so no edit made on the event day itself can enter. Capped
    at `OsmParams.latest_snapshot`, which is the newest state the instance
    holds: for a judged event in the past that cap is irrelevant, and for one
    near today it is the cap that binds.
    """
    p = params or OsmParams()
    cap = date.fromisoformat(p.latest_snapshot)
    chosen = min(event_day - timedelta(days=1), cap)
    if chosen < OHSOME_HISTORY_START:
        raise OsmError(
            f"no OpenStreetMap snapshot exists before {event_day}: the latest date strictly "
            f"before it is {chosen}, and the ohsome instance's history starts at "
            f"{OHSOME_HISTORY_START}. OSM cannot be used for an event this old; run with the "
            f"flood map only, and report the damage overlay as NOT AVAILABLE rather than zero.")
    return chosen


def buffered_bbox(bbox, buffer_km: float) -> tuple[float, float, float, float]:
    """Grow a WGS84 bbox by `buffer_km` on every side.

    Degrees per km from the local meridian and parallel: latitude is a constant
    1/110.574 deg/km, longitude divides by cos(latitude), evaluated at the edge
    NEAREST the pole so the buffer is never short of the requested distance
    anywhere in the box. Clamped to valid coordinates.
    """
    w, s, e, n = (float(v) for v in bbox)
    if buffer_km <= 0:
        return (w, s, e, n)
    dlat = buffer_km / 110.574
    worst_lat = max(abs(s), abs(n))
    # cos -> 0 at the pole; clamp so a near-polar box widens to the whole world
    # rather than dividing by zero.
    cos_lat = max(math.cos(math.radians(min(worst_lat + dlat, 89.9))), 1e-6)
    dlon = buffer_km / (111.320 * cos_lat)
    return (max(w - dlon, -180.0), max(s - dlat, -90.0),
            min(e + dlon, 180.0), min(n + dlat, 90.0))


def bbox_key(bbox) -> str:
    """A short stable key for a bbox, for cache filenames.

    Rounded to 6 decimals (about 0.1 m) before hashing, so float noise in an
    otherwise identical bbox cannot miss the cache.
    """
    rounded = ",".join(f"{float(v):.6f}" for v in bbox)
    return hashlib.sha256(rounded.encode()).hexdigest()[:12]


def cache_path(cache: Path, layer: str, snapshot: date, bbox) -> Path:
    return cache / f"osm_{layer}_{snapshot.isoformat()}_{bbox_key(bbox)}.geojson"


# --- reducing a full-history response to one snapshot -------------------------

def snapshot_features(features: list[dict], window_end: str | None = None) -> list[dict]:
    """One feature per OSM element, as at the END of a full-history window.

    Pure, and the reason the fallback can be trusted: a full-history response
    carries every version valid at any point in the window, so taking it as-is
    would double-count an element edited inside the window and would resurrect
    one deleted inside it.

      * per `@osmId`, the version with the latest `@validFrom` is the state at
        the window end;
      * with `window_end` given, a surviving version must also have
        `@validTo` == `window_end`. An element deleted mid-window has an
        earlier `@validTo` and is dropped, which is correct: it did not exist
        at the snapshot.

    Features with no `@osmId` (a snapshot-endpoint response has them, but a
    caller may pass anything) are kept as-is rather than silently dropped.
    """
    best: dict[str, dict] = {}
    anonymous: list[dict] = []
    for feat in features:
        props = feat.get("properties") or {}
        oid = props.get(OSM_ID)
        if oid is None:
            anonymous.append(feat)
            continue
        if window_end is not None and VALID_TO in props:
            if str(props[VALID_TO]) != str(window_end):
                continue
        current = best.get(oid)
        if current is None:
            best[oid] = feat
            continue
        if str(props.get(VALID_FROM, "")) >= str(
                (current.get("properties") or {}).get(VALID_FROM, "")):
            best[oid] = feat
    # Sorted by id so the written GeoJSON is byte-identical across runs (Rule 5).
    return anonymous + [best[k] for k in sorted(best)]


# --- talking to ohsome --------------------------------------------------------

@dataclass
class OsmRequest:
    """One attempt at one endpoint, kept so the report can say what was used."""

    endpoint: str
    url: str
    params: dict
    window_end: str | None = None


def _attempts(snapshot: date, bbox, osm_filter: str, p: OsmParams) -> list[OsmRequest]:
    bboxes = ",".join(f"{float(v):.6f}" for v in bbox)
    instant = f"{snapshot.isoformat()}T00:00:00Z"
    start = (snapshot - timedelta(days=FULL_HISTORY_WINDOW_DAYS)).isoformat()
    return [
        OsmRequest("elements/geometry", f"{p.base_url}/elements/geometry",
                   {"bboxes": bboxes, "filter": osm_filter,
                    "time": snapshot.isoformat(), "properties": "tags"}),
        OsmRequest("elementsFullHistory/geometry",
                   f"{p.base_url}/elementsFullHistory/geometry",
                   {"bboxes": bboxes, "filter": osm_filter,
                    "time": f"{start},{snapshot.isoformat()}", "properties": "tags"},
                   window_end=instant),
    ]


def _get(req: OsmRequest, p: OsmParams, session=None) -> dict:
    """One endpoint, with retries on transport errors. Raises OsmError."""
    http = session or requests
    headers = {"User-Agent": p.user_agent}
    last = ""
    for attempt in range(p.max_retries + 1):
        try:
            resp = http.get(req.url, params=req.params, headers=headers, timeout=p.timeout_s)
        except Exception as err:          # transport: worth retrying
            last = f"{type(err).__name__}: {err}"
            if attempt < p.max_retries:
                _time.sleep(p.retry_backoff_s * (attempt + 1))
                continue
            raise OsmError(
                f"could not reach the ohsome API at {req.url}: {last}. Check the internet "
                f"connection and https://ohsome.org status, then run again; the pipeline "
                f"caches OSM per area and snapshot, so nothing already downloaded is lost."
            ) from err
        if resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError as err:
                raise OsmError(
                    f"ohsome returned 200 but not JSON from {req.endpoint} "
                    f"({len(resp.content)} bytes, starts {resp.text[:80]!r}). This is a "
                    f"service-side problem; try again, or reduce the area.") from err
            if "features" not in body:
                raise OsmError(
                    f"ohsome response from {req.endpoint} has no 'features' key "
                    f"(keys: {sorted(body)[:8]}). Refusing to treat it as an empty area.")
            return body
        if resp.status_code in (413, 414, 504):
            raise OsmError(
                f"ohsome refused the request as too large ({resp.status_code} from "
                f"{req.endpoint}). The area plus its {p.buffer_km:g} km buffer is too big for "
                f"one OSM query. Run on a smaller bbox; the flood map itself is unaffected.")
        last = f"HTTP {resp.status_code}: {resp.text[:160]}"
        break
    raise OsmError(f"ohsome {req.endpoint} failed - {last}")


def fetch_layer(layer: str, bbox, snapshot: date, params: OsmParams | None = None,
                session=None, log=print) -> tuple[dict, dict]:
    """One OSM layer at one snapshot, as (GeoJSON, provenance). No cache.

    Tries the snapshot endpoint, then the full-history fallback (see the module
    docstring). If BOTH fail, the errors of both are reported together: a
    refusal that names only the last thing tried sends the reader down the
    wrong path.
    """
    p = params or OsmParams()
    if layer not in OSM_LAYERS:
        raise OsmError(f"unknown OSM layer {layer!r}; declared layers are "
                       f"{sorted(OSM_LAYERS)} (strandline.config.OSM_LAYERS)")
    osm_filter = OSM_LAYERS[layer]
    problems = []
    for req in _attempts(snapshot, bbox, osm_filter, p):
        try:
            body = _get(req, p, session=session)
        except OsmError as err:
            problems.append(f"{req.endpoint}: {err}")
            continue
        raw = list(body.get("features") or [])
        features = snapshot_features(raw, req.window_end)
        log(f"      OSM {layer}: {len(features)} feature(s) at {snapshot} "
            f"via {req.endpoint}")
        return ({"type": "FeatureCollection", "features": features},
                {"layer": layer, "filter": osm_filter, "snapshot": snapshot.isoformat(),
                 "endpoint": req.endpoint, "n_features": len(features),
                 "n_versions_returned": len(raw),
                 "attribution": "© OpenStreetMap contributors"})
    raise OsmError(
        f"could not read OSM layer {layer!r} for this area at snapshot {snapshot}. "
        f"Every endpoint failed:\n  " + "\n  ".join(problems) +
        f"\nThe flood map does not depend on OSM; re-run without the damage overlay to get "
        f"it, and report the damage and cut-off analysis as NOT AVAILABLE rather than zero.")


def load_layer(layer: str, bbox, snapshot: date, cache: Path,
               params: OsmParams | None = None, allow_download: bool = True,
               session=None, log=print) -> tuple[dict, dict]:
    """One layer, from the cache if present (Rule 5: cache by bbox + snapshot)."""
    path = cache_path(cache, layer, snapshot, bbox)
    if path.exists():
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise OsmError(
                f"cached OSM layer {path.name} cannot be read ({type(err).__name__}). "
                f"Delete it and run again to re-download.") from err
        log(f"      OSM {layer}: {len(body.get('features') or [])} feature(s) from cache "
            f"{path.name}")
        return body, {"layer": layer, "filter": OSM_LAYERS.get(layer),
                      "snapshot": snapshot.isoformat(), "endpoint": "cache",
                      "n_features": len(body.get("features") or []),
                      "cache": path.name,
                      "attribution": "© OpenStreetMap contributors"}
    if not allow_download:
        raise OsmError(
            f"OSM layer {layer!r} is not cached for this area at snapshot {snapshot} "
            f"({path.name}) and downloading is disabled. Run once with downloads allowed.")
    body, prov = fetch_layer(layer, bbox, snapshot, params, session=session, log=log)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part.geojson")
    tmp.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    prov["cache"] = path.name
    return body, prov


@dataclass
class OsmSnapshot:
    """Every OSM layer for one area and event, plus where it came from."""

    snapshot: date
    bbox_requested: tuple
    bbox_queried: tuple
    buffer_km: float
    layers: dict = field(default_factory=dict)       # layer -> GeoJSON dict
    provenance: dict = field(default_factory=dict)   # layer -> provenance dict

    def counts(self) -> dict[str, int]:
        return {k: len(v.get("features") or []) for k, v in sorted(self.layers.items())}

    def report(self) -> dict:
        """What goes into run_report.json under `osm`."""
        return {
            "snapshot": self.snapshot.isoformat(),
            "snapshot_rule": ("latest date strictly before the event date, capped at "
                              "OsmParams.latest_snapshot; OSM edits after the event are "
                              "never read (Rule 1)"),
            "bbox_requested": list(self.bbox_requested),
            "bbox_queried": list(self.bbox_queried),
            "buffer_km": self.buffer_km,
            "feature_counts": self.counts(),
            "layers": {k: self.provenance.get(k) for k in sorted(self.layers)},
            "attribution": "© OpenStreetMap contributors",
        }


def load_snapshot(bbox, event_day: date, cache: Path, params: OsmParams | None = None,
                  layers=None, allow_download: bool = True, session=None,
                  log=print) -> OsmSnapshot:
    """Every declared OSM layer for `bbox` + buffer, at the pre-event snapshot."""
    p = params or OsmParams()
    snapshot = snapshot_date(event_day, p)
    queried = buffered_bbox(bbox, p.buffer_km)
    wanted = list(layers) if layers else list(OSM_LAYERS)
    log(f"      OSM snapshot {snapshot} (event {event_day}), bbox + {p.buffer_km:g} km buffer")
    out = OsmSnapshot(snapshot=snapshot, bbox_requested=tuple(float(v) for v in bbox),
                      bbox_queried=queried, buffer_km=p.buffer_km)
    for layer in wanted:
        body, prov = load_layer(layer, queried, snapshot, cache, p,
                                allow_download=allow_download, session=session, log=log)
        out.layers[layer] = body
        out.provenance[layer] = prov
    return out


# --- handing layers to the overlay -------------------------------------------

def to_geodataframe(body: dict, crs_to=None):
    """A GeoJSON FeatureCollection as a GeoDataFrame, reprojected if asked.

    Empty in, empty out - with the right CRS and no columns invented, so a
    caller can treat "no buildings here" and "many buildings here" the same way
    rather than branching. Imported locally: geopandas pulls in GDAL and this
    module is also used for its pure functions.
    """
    import geopandas as gpd

    features = list(body.get("features") or [])
    if not features:
        gdf = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    return gdf if crs_to is None else gdf.to_crs(crs_to)
