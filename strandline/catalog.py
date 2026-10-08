"""Pre-flight: find Sentinel-1 GRD scenes and pick the best same-orbit track.

Change detection is only valid between images from the SAME relative orbit
(same viewing geometry). This module searches the Copernicus Data Space
catalogue (OData, no login needed), groups scenes into acquisitions per
track, and chooses the track with the earliest post-event image and enough
pre-event images to build a reference stack.

If no track qualifies, it says so plainly instead of producing a bad map.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import requests
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from strandline.config import RunParams

ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"


@dataclass(frozen=True)
class Scene:
    name: str
    start: datetime
    stop: datetime
    platform: str               # "S1A", "S1C", ...
    relative_orbit: int | None
    direction: str | None       # "ASCENDING" / "DESCENDING"
    footprint: BaseGeometry     # EPSG:4326


@dataclass
class Acquisition:
    """All scenes of one track on one UTC date (adjacent slices of a pass)."""
    track: tuple
    day: date
    start: datetime
    stop: datetime
    names: list[str]
    footprint: BaseGeometry
    coverage: float = 0.0


@dataclass
class TrackChoice:
    track: tuple
    post: Acquisition
    pre: list[Acquisition]
    latency_days: int
    notes: list[str] = field(default_factory=list)


# --- Parsing -----------------------------------------------------------------

def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _relative_orbit_from_name(name: str) -> int | None:
    """Fallback for S1A only (offset known): rel = (abs - 73) % 175 + 1."""
    parts = name.split("_")
    if not name.startswith("S1A") or len(parts) < 7:
        return None
    try:
        absolute = int(parts[6])
    except ValueError:
        return None
    return (absolute - 73) % 175 + 1


def parse_product(item: dict) -> Scene:
    attrs = {a.get("Name"): a.get("Value") for a in item.get("Attributes", [])}
    name = item["Name"]
    rel = attrs.get("relativeOrbitNumber")
    rel = int(rel) if rel is not None else _relative_orbit_from_name(name)
    direction = attrs.get("orbitDirection")
    platform = name[:3]
    return Scene(
        name=name,
        start=_parse_time(item["ContentDate"]["Start"]),
        stop=_parse_time(item["ContentDate"]["End"]),
        platform=platform,
        relative_orbit=rel,
        direction=direction.upper() if isinstance(direction, str) else None,
        footprint=shape(item["GeoFootprint"]),
    )


def deduplicate(scenes: list[Scene]) -> list[Scene]:
    """The catalogue lists some products twice (e.g. SAFE and COG variants)."""
    seen: dict[tuple, Scene] = {}
    for s in scenes:
        key = (s.platform, s.start.replace(microsecond=0))
        is_plain_safe = s.name.endswith(".SAFE") and "_COG" not in s.name
        if key not in seen or is_plain_safe:
            seen[key] = s
    return sorted(seen.values(), key=lambda s: s.start)


# --- Search ------------------------------------------------------------------

def search_s1_grd(bbox: tuple[float, float, float, float], start: date, end: date,
                  session: requests.Session | None = None, timeout: int = 60) -> list[Scene]:
    w, s, e, n = bbox
    wkt = f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"
    flt = (
        "Collection/Name eq 'SENTINEL-1' and contains(Name,'IW_GRDH_1SDV') "
        f"and OData.CSC.Intersects(area=geography'SRID=4326;{wkt}') "
        f"and ContentDate/Start ge {start.isoformat()}T00:00:00.000Z "
        f"and ContentDate/Start lt {end.isoformat()}T00:00:00.000Z"
    )
    params = {"$filter": flt, "$expand": "Attributes", "$top": 200,
              "$orderby": "ContentDate/Start asc"}
    http = session or requests.Session()
    url, scenes = ODATA_URL, []
    while url:
        resp = http.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        scenes += [parse_product(item) for item in payload.get("value", [])]
        url, params = payload.get("@odata.nextLink"), None
    return deduplicate(scenes)


# --- Track selection ---------------------------------------------------------

def track_key(scene: Scene) -> tuple:
    if scene.relative_orbit is not None:
        return ("orbit", scene.relative_orbit, scene.direction)
    # Unknown orbit number: same track passes at ~the same time of day.
    minutes = scene.start.hour * 60 + scene.start.minute
    return ("time", round(minutes / 5) * 5, scene.direction)


def group_acquisitions(scenes: list[Scene], aoi: BaseGeometry) -> list[Acquisition]:
    groups: dict[tuple, list[Scene]] = {}
    for s in scenes:
        groups.setdefault((track_key(s), s.start.date()), []).append(s)
    acqs = []
    for (track, day), members in groups.items():
        fp = unary_union([m.footprint for m in members])
        acqs.append(Acquisition(
            track=track, day=day,
            start=min(m.start for m in members), stop=max(m.stop for m in members),
            names=[m.name for m in members], footprint=fp,
            coverage=float(fp.intersection(aoi).area / aoi.area) if aoi.area else 0.0,
        ))
    return sorted(acqs, key=lambda a: (a.track, a.start))


def choose_track(acqs: list[Acquisition], event_day: date, params: RunParams
                 ) -> tuple[TrackChoice | None, list[str]]:
    """Returns (best choice or None, human-readable log of every track)."""
    log, candidates = [], []
    first_post_day = event_day if params.post_same_day_ok else event_day + timedelta(days=1)
    tracks = sorted({a.track for a in acqs})
    for track in tracks:
        mine = [a for a in acqs if a.track == track and a.coverage >= params.min_aoi_coverage]
        posts = [a for a in mine if a.day >= first_post_day
                 and (a.day - event_day).days <= params.max_post_latency_days]
        pres = [a for a in mine if a.day < event_day][-params.n_pre:]
        label = f"track {track[1]} {track[2] or '?'}"
        if not posts:
            log.append(f"{label}: no full-coverage post-event image within "
                       f"{params.max_post_latency_days} days -> skipped")
            continue
        if len(pres) < params.min_pre:
            log.append(f"{label}: only {len(pres)} pre-event images (need {params.min_pre}) -> skipped")
            continue
        post = posts[0]
        latency = (post.day - event_day).days
        log.append(f"{label}: post {post.day} (+{latency} d), {len(pres)} pre images "
                   f"{pres[0].day}..{pres[-1].day} -> OK")
        candidates.append(TrackChoice(track=track, post=post, pre=pres, latency_days=latency))

    if not candidates:
        return None, log
    best = min(candidates, key=lambda c: (c.latency_days, -len(c.pre), -c.post.coverage))
    same_day_other = [a for a in acqs if a.day == best.post.day and a.track != best.track]
    if same_day_other:
        best.notes.append("another track also imaged the area on the post-event day; "
                          "time-window filtering keeps them apart, but verify visually")
    return best, log


def aoi_polygon(bbox: tuple[float, float, float, float]) -> BaseGeometry:
    return box(*bbox)
