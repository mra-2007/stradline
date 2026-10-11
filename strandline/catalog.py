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

from strandline.config import OpticalParams, RunParams

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


# --- Sentinel-2 L2A ----------------------------------------------------------
#
# Optical evidence is only worth downloading when a date is actually clear, and
# the catalogue already knows: every L2A product carries a `cloudCover`
# attribute. So scene choice happens here, on metadata alone, and a hopelessly
# clouded area costs nothing but one catalogue query.
#
# Choice is made per UTC DAY, not per scene. openEO mosaics every tile it finds
# in a one-day temporal extent, so the unit that can be downloaded and used is
# a day, and a day's cloudiness is the area-weighted mean of the scenes that
# actually overlap the AOI.


@dataclass(frozen=True)
class S2Scene:
    name: str
    start: datetime
    cloud_cover: float | None    # percent, from the catalogue
    footprint: BaseGeometry      # EPSG:4326


@dataclass
class S2Day:
    """Every L2A scene overlapping the AOI on one UTC date."""
    day: date
    start: datetime
    names: list[str]
    footprint: BaseGeometry
    coverage: float = 0.0        # share of the AOI the scenes cover
    cloud_cover: float = 100.0   # area-weighted mean percent over the AOI


@dataclass
class S2Pair:
    pre: S2Day
    post: S2Day
    post_latency_days: int
    notes: list[str] = field(default_factory=list)


def parse_s2_product(item: dict) -> S2Scene:
    attrs = {a.get("Name"): a.get("Value") for a in item.get("Attributes", [])}
    cloud = attrs.get("cloudCover")
    try:
        cloud = None if cloud is None else float(cloud)
    except (TypeError, ValueError):
        cloud = None
    return S2Scene(
        name=item["Name"],
        start=_parse_time(item["ContentDate"]["Start"]),
        cloud_cover=cloud,
        footprint=shape(item["GeoFootprint"]),
    )


def search_s2_l2a(bbox: tuple[float, float, float, float], start: date, end: date,
                  session: requests.Session | None = None, timeout: int = 60) -> list[S2Scene]:
    """Sentinel-2 L2A products intersecting `bbox` in [start, end). Metadata only."""
    w, s, e, n = bbox
    wkt = f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"
    flt = (
        "Collection/Name eq 'SENTINEL-2' and contains(Name,'MSIL2A') "
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
        scenes += [parse_s2_product(item) for item in payload.get("value", [])]
        url, params = payload.get("@odata.nextLink"), None
    unique = {sc.name: sc for sc in scenes}
    return sorted(unique.values(), key=lambda sc: (sc.start, sc.name))


def group_s2_days(scenes: list[S2Scene], aoi: BaseGeometry) -> list[S2Day]:
    """Group scenes by UTC date; AOI coverage and area-weighted cloud per date.

    A scene whose cloudCover the catalogue does not report is weighted in as
    100% cloudy: unknown cloudiness is never treated as clear.
    """
    groups: dict[date, list[S2Scene]] = {}
    for sc in scenes:
        groups.setdefault(sc.start.date(), []).append(sc)
    days = []
    for day, members in groups.items():
        fp = unary_union([m.footprint for m in members])
        overlaps = [(m, m.footprint.intersection(aoi).area) for m in members]
        weight = sum(a for _, a in overlaps)
        cloud = (sum((100.0 if m.cloud_cover is None else m.cloud_cover) * a
                     for m, a in overlaps) / weight) if weight > 0 else 100.0
        days.append(S2Day(
            day=day, start=min(m.start for m in members),
            names=sorted(m.name for m in members), footprint=fp,
            coverage=float(fp.intersection(aoi).area / aoi.area) if aoi.area else 0.0,
            cloud_cover=float(cloud),
        ))
    return sorted(days, key=lambda d: d.day)


def choose_s2_pair(days: list[S2Day], event_day: date, op: OpticalParams
                   ) -> tuple[S2Pair | None, list[str]]:
    """The clearest usable pre-event and post-event date, or None with a reason.

    Clearest wins, ties broken toward the date closest to the event: a cloudy
    scene yields nothing at all, while a later clear one yields evidence whose
    latency is recorded and reported.
    """
    log: list[str] = []
    first_post = event_day if op.post_same_day_ok else event_day + timedelta(days=1)
    earliest_pre = event_day - timedelta(days=op.search_days_before)
    last_post = event_day + timedelta(days=op.max_post_latency_days)

    def usable(window: list[S2Day], label: str) -> list[S2Day]:
        keep = []
        for d in window:
            if d.coverage < op.min_aoi_coverage:
                log.append(f"{label} {d.day}: covers {d.coverage:.0%} of the AOI "
                           f"(need {op.min_aoi_coverage:.0%}) -> skipped")
            elif d.cloud_cover > op.max_scene_cloud_cover:
                log.append(f"{label} {d.day}: {d.cloud_cover:.0f}% cloud "
                           f"(ceiling {op.max_scene_cloud_cover:.0f}%) -> skipped")
            else:
                keep.append(d)
                log.append(f"{label} {d.day}: {d.cloud_cover:.0f}% cloud, "
                           f"{d.coverage:.0%} coverage -> candidate")
        return keep

    pres = usable([d for d in days if earliest_pre <= d.day < event_day], "pre ")
    posts = usable([d for d in days if first_post <= d.day <= last_post], "post")
    if not pres:
        log.append(f"no usable clear-enough Sentinel-2 date in the "
                   f"{op.search_days_before} days before the event")
        return None, log
    if not posts:
        log.append(f"no usable clear-enough Sentinel-2 date within "
                   f"{op.max_post_latency_days} days after the event")
        return None, log

    pre = min(pres, key=lambda d: (d.cloud_cover, (event_day - d.day).days))
    post = min(posts, key=lambda d: (d.cloud_cover, (d.day - event_day).days))
    latency = (post.day - event_day).days
    log.append(f"chosen: pre {pre.day} ({pre.cloud_cover:.0f}% cloud), "
               f"post {post.day} (+{latency} d, {post.cloud_cover:.0f}% cloud)")
    notes = []
    if latency > RunParams.max_post_latency_days:
        notes.append(
            f"the optical post-event date is +{latency} d, later than the radar post "
            f"image: standing water may have drained, so optical water evidence is a "
            f"lower bound and deposits are the stronger optical signal")
    return S2Pair(pre=pre, post=post, post_latency_days=latency, notes=notes), log
