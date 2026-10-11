"""All tunable parameters in one place.

Rule: these defaults are tuned on OTHER floods, never on the Trishuli case
study, which stays a held-out test. Every run writes the exact values it
used into run_report.json, so any result can be reproduced.
"""

from dataclasses import asdict, dataclass


# Detection modes. Both stay available; the default is the one that has been
# in use, so a comparison is always against the real baseline.
#
#   darkening    open water is dark and darkens: specular scattering from a
#                flat water surface. True on a floodplain.
#   rough_water  a confined, turbulent, sediment-laden flood in a gorge is a
#                ROUGH surface at C-band and can scatter MORE than the dry
#                valley floor it replaced, so the SIGN of the change is not a
#                reliable flood signature there and the magnitude of a coherent
#                change low on the valley floor is used instead. Darkness is
#                kept only as the high-confidence criterion.
#                See docs/tuning_log.md, 2026-10-11.
MODE_DARKENING = "darkening"
MODE_ROUGH_WATER = "rough_water"
DETECT_MODES = (MODE_DARKENING, MODE_ROUGH_WATER)


@dataclass(frozen=True)
class DetectParams:
    # Which detection mode (see DETECT_MODES above). Chosen by eval/tune.py
    # over the dev set, never per area or per event.
    mode: str = MODE_DARKENING

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

    # --- rough_water mode only -------------------------------------------
    # Minimum absolute backscatter change, in dB, in BOTH polarisations, for a
    # pixel to count as changed at all. Sentinel-1 GRD radiometric stability is
    # of order 0.5 dB, so a z-score alone can call a 0.75 dB wobble a change
    # (z_grow 1.5 x std_floor_db 0.5): that is what made the old debris channel
    # fire on 3.8% of an AOI. 3 dB is a factor of 2 in power and the long-
    # standing convention in SAR change detection. Not set from any dev-event
    # score; see docs/tuning_log.md, 2026-10-11.
    min_abs_change_db: float = 3.0

    # Both polarisations must move in the SAME direction for a seed. A real
    # surface change moves VV and VH together; speckle does not.
    require_pol_agreement: bool = True

    # Cleanup.
    mmu_pixels: int = 6                     # minimum mapping unit (water)
    mmu_pixels_debris: int = 10             # debris fans are larger
    require_drainage_connection: bool = True
    drainage_buffer_px: int = 3

    def __post_init__(self) -> None:
        if self.mode not in DETECT_MODES:
            raise ValueError(
                f"detect mode {self.mode!r} is not supported; "
                f"choose one of {', '.join(DETECT_MODES)}."
            )


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

# THE PRIMARY SCORED QUANTITY: flood-affected area.
#
# Track B asks for flooded AND debris-covered areas, so what the gates measure
# is the union of our flood and debris classes, scored against each reference's
# flooded + debris/mass-movement classes (that event's `reference_classes`).
# Water-only scores are still computed and reported alongside.
#
# Decided and recorded in docs/tuning_log.md on 2026-10-11, BEFORE the new mode
# was scored. Note it makes the task harder on five of the six independent
# events: only one declares a reference debris class, so for the others the
# reference union is unchanged while our prediction gains the debris class.
FLOOD_AFFECTED_CLASSES = (CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM, CLASS_DEBRIS)


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


# --- Parameter search grids (eval/tune.py, leave-one-event-out) --------------
#
# Rule 3.4: parameter values are chosen by eval/tune.py with leave-one-event-out,
# or set from physics or literature without looking at scores. Never hand-tuned.
# Rule 3.7: the grid itself is a tunable, so it lives here.
#
# One grid PER DETECTION MODE, so no point ever varies a parameter its mode
# ignores. Keys are "<section>.<field>", section being "run" (RunParams) or
# "detect" (DetectParams). The search is exhaustive over each mode's cartesian
# product, so the grids are deliberately small: every point costs one detection
# pass per event.
#
# DECLARED BEFORE THE NEW MODE WAS SCORED, with every value justified in
# docs/tuning_log.md (2026-10-11):
#
#   z_seed             2.5-3.5 sigma, the range in the multi-temporal change
#                      literature this detector follows (DeVries et al. 2020).
#   hand_max_water_m   in BOTH grids, so the scoreboard can say whether a gain
#                      came from the rough-water channel or merely from the
#                      terrain widening. 20 m is the current value; HAND-based
#                      floodplain delineation uses 5-30 m (Rennó et al. 2008;
#                      Nobre et al. 2011) and a confined gorge flood adds flow
#                      depth plus superelevation, so 35 m is the physical upper
#                      end.
#   water_db_max       darkening mode only; unchanged from the earlier grid.
#   min_abs_change_db  rough_water mode only; see the field's comment above.
#
# slope_max_water_deg is deliberately NOT widened and NOT searched: a water
# surface is flat, and a 20 m DEM slope above 15 deg on a valley floor is far
# more likely a wall pixel than a flooded one. Holding it fixed also keeps the
# two modes comparable on it.
#
# Everything else stays at its physics- or literature-set value: the Lee window
# and ENL describe the sensor, the minimum mapping units describe the
# resolution. Adding an axis must be justified here, not by a score.
TUNE_GRIDS: dict[str, dict[str, tuple]] = {
    MODE_DARKENING: {                                    # 1 x 3 x 2 x 3 = 18
        "run.backscatter_coefficient": BACKSCATTER_COEFFICIENTS,
        "detect.z_seed": (2.5, 3.0, 3.5),
        "detect.hand_max_water_m": (20.0, 35.0),
        "detect.water_db_max": (-18.0, -15.0, -12.0),
    },
    MODE_ROUGH_WATER: {                                  # 1 x 3 x 2 x 2 = 12
        "run.backscatter_coefficient": BACKSCATTER_COEFFICIENTS,
        "detect.z_seed": (2.5, 3.0, 3.5),
        "detect.hand_max_water_m": (20.0, 35.0),
        "detect.min_abs_change_db": (2.0, 3.0),
    },
}
