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
    use_sar_mask: bool = False     # layover/shadow band: verify encoding first


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
