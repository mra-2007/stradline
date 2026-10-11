"""All tunable parameters in one place.

Rule: these defaults are tuned on OTHER floods, never on the Trishuli case
study, which stays a held-out test. Every run writes the exact values it
used into run_report.json, so any result can be reproduced.
"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DetectParams:
    # Speckle filter (Lee), applied to linear backscatter before dB conversion.
    lee_window: int = 5
    enl: float = 4.4  # equivalent number of looks, Sentinel-1 IW GRDH

    # Temporal z-score: z = (post - mean_pre) / max(std_pre, std_floor_db).
    # The floor stops tiny std estimates (few pre images) inflating z.
    std_floor_db: float = 0.5

    # Hysteresis region growing on the combined z-score.
    z_seed: float = 3.0        # strong change: starts a region
    z_grow: float = 1.5        # weaker change: joins a region only if connected
    z_pol_agree: float = 2.0   # both VV and VH below -this => high confidence

    # Open-water darkness threshold on post-event VV (dB), found by Otsu
    # inside the valley floor, then clamped to a physically plausible range.
    water_db_min: float = -24.0
    water_db_max: float = -15.0
    grow_darkness_margin_db: float = 3.0

    # Terrain limits (from the DEM). Water lies on flat valley floors; debris
    # can sit higher and steeper. HAND = height above nearest drainage.
    hand_max_water_m: float = 20.0
    hand_max_debris_m: float = 35.0
    slope_max_water_deg: float = 15.0
    slope_max_debris_deg: float = 30.0

    # Debris must look like rough rock AFTER the event (bright in VV) and
    # brighten in BOTH polarisations. Stops wind-roughened rivers and
    # speckle at river banks being called debris.
    debris_min_post_vv_db: float = -12.0

    # Cleanup.
    mmu_pixels: int = 6                     # minimum mapping unit (water)
    mmu_pixels_debris: int = 10             # debris fans are larger
    require_drainage_connection: bool = True
    drainage_buffer_px: int = 3


# Sentinel-1 backscatter coefficients we support, as openEO names them.
#
#   sigma0-ellipsoid  radiometric reference area from the ellipsoid. Terrain
#                     still drives much of the brightness, so backscatter is
#                     dominated by geometry in steep country; for same-orbit
#                     change detection that term largely cancels between dates.
#
# gamma0-terrain (radiometric terrain flattening against the DEM) was listed
# here until 2026-10-11 and has been REMOVED: the Copernicus Data Space openEO
# backend does not offer it. Its own `sar_backscatter` process description
# declares coefficient as enum ["sigma0-ellipsoid"], and a request for
# gamma0-terrain is refused before a batch job is even created:
#
#     OpenEoClientException: Invalid `sar_backscatter` coefficient
#     'gamma0-terrain'. Should be one of ['sigma0-ellipsoid']
#
# So terrain flattening is not a choice we have on this backend, and the
# tuning question it was raised for cannot be answered here (docs/tuning_log.md,
# 2026-10-11). Put the name back only after the backend's own process schema
# advertises it; TUNE_GRID then searches it again with no further change.
BACKSCATTER_COEFFICIENTS = ("sigma0-ellipsoid",)

# DEM used by openEO's sar_backscatter for orthorectification. The backend
# default (None) resolves to SRTMGL1, which ENDS AT 60 deg N: every Norwegian
# dev event failed with FileNotFoundError on tile N61E009 until this was set.
# COPERNICUS_30 is global and is the same DEM the terrain stage uses, so
# geocoding and HAND now rest on one elevation source rather than two.
#
# It is part of the processing, not a preference: it changes where pixels land,
# so it belongs in params_hash and in every cache filename.
ELEVATION_MODELS = ("COPERNICUS_30",)


@dataclass(frozen=True)
class RunParams:
    resolution_m: float = 20.0
    n_pre: int = 5                 # pre-event images in the reference stack
    min_pre: int = 3               # refuse to run with fewer
    max_post_latency_days: int = 12
    post_same_day_ok: bool = False  # event time unknown: use next day onward
    min_aoi_coverage: float = 0.95
    search_days_before: int = 75
    stream_threshold_km2: float = 5.0  # upstream area that defines a channel
    backscatter_coefficient: str = "sigma0-ellipsoid"
    elevation_model: str = "COPERNICUS_30"
    use_sar_mask: bool = False     # layover/shadow band: verify encoding first

    def __post_init__(self) -> None:
        if self.backscatter_coefficient not in BACKSCATTER_COEFFICIENTS:
            raise ValueError(
                f"backscatter_coefficient {self.backscatter_coefficient!r} is not supported; "
                f"choose one of {', '.join(BACKSCATTER_COEFFICIENTS)}."
            )
        if self.elevation_model not in ELEVATION_MODELS:
            raise ValueError(
                f"elevation_model {self.elevation_model!r} is not supported; "
                f"choose one of {', '.join(ELEVATION_MODELS)}."
            )


def params_dict(detect: DetectParams, run: RunParams) -> dict:
    return {"detect": asdict(detect), "run": asdict(run)}


# Output class codes (uint8 GeoTIFF). Kept stable: dashboard, copilot and
# validation all read these.
CLASS_NODATA = 0
CLASS_NO_CHANGE = 1
CLASS_FLOOD_HIGH = 2
CLASS_FLOOD_MEDIUM = 3
CLASS_DEBRIS = 4
CLASS_PERMANENT_WATER = 5
CLASS_TERRAIN_EXCLUDED = 6

CLASS_NAMES = {
    CLASS_NODATA: "no data",
    CLASS_NO_CHANGE: "no significant change",
    CLASS_FLOOD_HIGH: "flood water (high confidence)",
    CLASS_FLOOD_MEDIUM: "flood water (medium confidence)",
    CLASS_DEBRIS: "possible debris / deposit",
    CLASS_PERMANENT_WATER: "permanent water",
    CLASS_TERRAIN_EXCLUDED: "not analysed (steep or high terrain)",
}

FLOOD_CLASSES = (CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM)


# --- Dev-event scoring cost ---------------------------------------------------
#
# Scoring the dev set means downloading a Sentinel-1 stack per event per
# backscatter coefficient. DEV_N_PRE trades reference-stack depth for download
# time and openEO quota: 3 is `RunParams.min_pre`, the thinnest stack the
# pipeline will accept, and one dev event (the steepest) has only 3 pre images
# available anyway.
#
# This is a COST setting, not a tuned value, and it is not free: a 3-image
# reference stack gives a noisier per-pixel std than 5, so `std_floor_db` does
# more of the work and z-scores are less stable. Numbers measured at
# DEV_N_PRE therefore describe a thinner stack than the shipped default
# (RunParams.n_pre = 5) and must be reported as such. Every params_hash
# records n_pre, so any row can be traced back to the stack depth used.
DEV_N_PRE = 3


# --- Parameter search grid (eval/tune.py, leave-one-event-out) ----------------
#
# Rule 3.4: parameter values are chosen by eval/tune.py with leave-one-event-out,
# or set from physics or literature without looking at scores. Never hand-tuned.
# Rule 3.7: the grid itself is a tunable, so it lives here.
#
# Keys are "<section>.<field>", section being "run" (RunParams) or "detect"
# (DetectParams). The search is exhaustive over the cartesian product, so the
# grid is deliberately small: every axis costs one detection pass per event,
# and a backscatter axis costs a whole extra download of the dev set.
#
# Why these three axes and not others:
#   run.backscatter_coefficient  the open question from docs/tuning_log.md:
#                                real VV correlates with DEM-simulated terrain
#                                brightness at r = 0.75, so radiometric terrain
#                                flattening may separate the valley-floor
#                                histogram that sigma0 leaves unimodal.
#   detect.z_seed                the multi-temporal change threshold. 2.5-3.5
#                                sigma is the range used in the literature this
#                                detector follows (DeVries et al. 2020).
#   detect.water_db_max          the ceiling on the open-water darkness gate,
#                                which the tuning log shows is the operative
#                                threshold in steep terrain (Otsu returns a
#                                land-like value there and the clamp bites).
#
# Everything else stays at its physics- or literature-set value: HAND and slope
# limits describe where water can lie, the Lee window and ENL describe the
# sensor, and the minimum mapping units describe the resolution. Adding an axis
# must be justified here, not by a score.
TUNE_GRID: dict[str, tuple] = {
    "run.backscatter_coefficient": BACKSCATTER_COEFFICIENTS,
    "detect.z_seed": (2.5, 3.0, 3.5),
    "detect.water_db_max": (-18.0, -15.0, -12.0),
}
