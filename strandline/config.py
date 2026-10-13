"""All tunable parameters in one place.

Rule: these defaults are tuned on OTHER floods, never on the Trishuli case
study, which stays a held-out test. Every run writes the exact values it
used into run_report.json, so any result can be reproduced.
"""

from dataclasses import asdict, dataclass
from datetime import date


# Detection modes. Both stay available. The default is `rough_water`, adopted
# on 2026-10-11 by the leave-one-event-out run in eval/chosen_params.json
# (run_id 20261011T110254Z): it wins on 5 of the 6 held-out flood events, and
# `darkening` turned out to be INFEASIBLE - all 18 of its grid points breach
# the 1% control ceiling (worst 4.9% false flood on a dry Norwegian valley).
# `darkening` is kept so that comparison remains runnable, and because it is
# the correct physics on a flat floodplain (it wins at Larkana).
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


# How Sentinel-2 optical evidence is allowed to act on the class map.
#
#   off         optical is still computed, written and reported, but contributes
#               no pixel to the classes. The map is byte-identical to a
#               SAR-only run.
#   union       an optical water OR deposit detection that passes the SAME
#               terrain, minimum-mapping-unit and drainage tests as a SAR
#               detection is added to the map; where BOTH sensors agree the
#               pixel is high confidence.
#   water_only  the MNDWI water evidence is fused exactly as in `union`, and
#               the NDVI-drop / SWIR-rise DEPOSIT channel contributes nothing.
#
# `water_only` was added on 2026-10-12 because the class-count audit localised
# the harm precisely: the optical deposit channel produced 7,111 false pixels
# on one dry control and scores precision 0.0 against the only expert
# mass-movement reference in the dev set, while the optical WATER channel is
# where the Larkana gain came from. It is the configuration that keeps the one
# and drops the other. Declared before it was scored (docs/tuning_log.md).
#
# `off` exists so the modes can be compared on the dev set, and so a run can be
# reproduced without optical even where a clear scene was available.
FUSION_OFF = "off"
FUSION_UNION = "union"
FUSION_WATER_ONLY = "water_only"
OPTICAL_FUSION_MODES = (FUSION_OFF, FUSION_UNION, FUSION_WATER_ONLY)

# The fusion modes in which optical evidence may add a WATER pixel, and those
# in which it may add a DEPOSIT pixel. Kept here rather than spelled out in
# `detect`, so there is one definition of what each mode means.
FUSION_WATER_MODES = (FUSION_UNION, FUSION_WATER_ONLY)
FUSION_DEBRIS_MODES = (FUSION_UNION,)


@dataclass(frozen=True)
class DetectParams:
    # Which detection mode (see DETECT_MODES above). Chosen by eval/tune.py
    # over the dev set, never per area or per event.
    mode: str = MODE_ROUGH_WATER

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


# Pre-event images in the temporal reference stack - ONE value, used both by a
# default run and by every dev-event score, which is the whole point of it
# being declared here.
#
# It was 5 for a default run while the dev set was scored at 3, so a default
# invocation produced a configuration that had never been measured and whose
# params_hash matched no published row. docs/accuracy_diagnosis.md measured
# that mismatch (2026-10-13) and it was the only one applying to every fold.
# A pipeline that ships a configuration nobody scored cannot say what its
# accuracy is, so the shipped default was brought to the measured value rather
# than the dev set raised to the shipped one - which would have invalidated
# every recorded score and cost another full download of the dev set.
#
# The cost is real and is NOT hidden: 3 is `RunParams.min_pre`, the thinnest
# stack the pipeline accepts, and a 3-image stack gives a noisier per-pixel std
# than 5, so `DetectParams.std_floor_db` does more of the work and z-scores are
# less stable. Whether 5 is actually better is UNMEASURED - no dev-event score
# exists at 5 - and answering it means a fresh download of the dev set at
# n_pre=5 and a leave-one-event-out pass. Until that is done, this value is
# what the published numbers describe.
#
# `DEV_N_PRE` below is an alias kept for the eval code that reads it by that
# name; tests/test_shipped_defaults.py asserts the two can never drift apart.
N_PRE = 3


@dataclass(frozen=True)
class RunParams:
    resolution_m: float = 20.0
    n_pre: int = N_PRE             # pre-event images in the reference stack (see N_PRE)
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


# --- Sentinel-2 optical evidence ---------------------------------------------
#
# Reflectance bands we load, all available at 20 m, plus the scene
# classification layer. Chosen for what each index needs and nothing more,
# because every extra band is another download:
#
#   B03 green, B11 SWIR1   MNDWI = (green - swir1) / (green + swir1)  [Xu 2006]
#   B04 red,   B08 NIR     NDVI  = (nir - red) / (nir + red)
#   B12 SWIR2              reported alongside B11 for the deposit signature
#
# SCL is a CLASS band: it must never be resampled bilinearly, which is why it
# is loaded as its own cube with nearest-neighbour resampling.
S2_REFLECTANCE_BANDS = ("B03", "B04", "B08", "B11", "B12")
S2_SCL_BAND = "SCL"

# Scene classification layer codes (Sentinel-2 L2A product specification).
SCL_NO_DATA = 0
SCL_SATURATED = 1
SCL_CAST_SHADOW = 2
SCL_CLOUD_SHADOW = 3
SCL_VEGETATION = 4
SCL_NOT_VEGETATED = 5
SCL_WATER = 6
SCL_UNCLASSIFIED = 7
SCL_CLOUD_MEDIUM = 8
SCL_CLOUD_HIGH = 9
SCL_THIN_CIRRUS = 10
SCL_SNOW_ICE = 11


@dataclass(frozen=True)
class OpticalParams:
    """Sentinel-2 optical evidence: which scenes, which pixels, which indices.

    Every threshold here is set from the product specification or from the
    remote-sensing literature, BEFORE any dev-event optical score existed
    (docs/tuning_log.md). The three that OPTICAL_TUNE_GRID searches are marked.
    """

    # Optical is ON by default: Track B requires Sentinel-2 to be used when the
    # sky is clear. A run with no clear pair is identical to a SAR-only run and
    # says so in run_report.json, so this default costs nothing under cloud.
    enabled: bool = True

    # What optical evidence is allowed to DO to the classes (see
    # OPTICAL_FUSION_MODES above). It lives here rather than in DetectParams so
    # that a radar-only configuration hashes exactly as it did before
    # Sentinel-2 existed in this pipeline - see params_dict, and
    # tests/test_params_hash_reproducible.py, which pins the published hashes.
    #
    # `union` since 2026-10-12, adopted by the leave-one-event-out optical pass
    # (eval/chosen_params.json, run_id 20261012T...; docs/tuning_log.md) against
    # the rule in OPTICAL_ADOPTION, which was declared before the pass was run.
    # It was `off` until then, deliberately, because shipping an unmeasured
    # change to the scored quantity and to the 1% false-flood ceiling is not a
    # trade this project makes.
    #
    # What was measured, held out, over 6 independent flood events and 4
    # controls: mean boundary-tolerant F1 0.5134 -> 0.5232 (+0.0098), no event
    # worse by more than 0.0033, no control above 0.0022 against the 1%
    # ceiling. READ THE CAVEAT: the gain is CONCENTRATED - +0.0548 on the one
    # flat, wholly cloud-free floodplain event (Larkana) and +0.0095 on Bosnia,
    # against -0.0023 and -0.0033 on two Himalayan/Norwegian events and exactly
    # 0.0000 on the two where cloud refused optical entirely. Optical helps
    # most where radar already worked, and does not fix the gorge failure that
    # keeps the flood gate at FAIL.
    fusion: str = FUSION_UNION

    # --- which scenes ------------------------------------------------------
    search_days_before: int = 60      # pre-event window to find a clear date
    # Mud and debris scars persist for weeks, and a flooded valley under cloud
    # on day 1 is often clear a fortnight later, so the post window is much
    # longer than the SAR one (RunParams.max_post_latency_days = 12). Standing
    # water may have drained by then: that costs us recall of WATER and is
    # reported as the post date's latency, never hidden.
    max_post_latency_days: int = 20
    post_same_day_ok: bool = False    # event time unknown, as for SAR
    min_aoi_coverage: float = 0.95    # same as RunParams: no partial AOIs

    # A COST gate, applied to catalogue metadata so a hopeless date is never
    # downloaded. Scene-level cloudCover is for the whole 110 km tile and says
    # little about our much smaller AOI, so it is set loose on purpose: the
    # real decision is min_clear_fraction, measured from SCL after download.
    max_scene_cloud_cover: float = 70.0

    # Minimum share of the AOI that must be clear - per date, and for the pair
    # (the two dates' clear masks intersected, which is what the indices need).
    # Below this, optical is reported as too cloudy and contributes nothing.
    min_clear_fraction: float = 0.20

    # SCL classes a pixel may have and still be used. Cloud (8, 9), cirrus
    # (10), both shadow classes (2, 3), snow and ice (11) and the defective
    # classes (0, 1) are all out.
    #
    # UNCLASSIFIED (7) is deliberately KEPT: L2A routinely assigns it to
    # turbid water and freshly flooded ground, which is exactly our target, so
    # dropping it would systematically remove the pixels we are looking for.
    # CAST_SHADOW (2) is deliberately DROPPED even though terrain shadow is
    # common in the gorges we care about and excluding it costs us coverage
    # there: a shadowed pixel is dark in every band and its indices are not
    # trustworthy. Coverage lost this way is reported, not silently filled.
    scl_valid_classes: tuple[int, ...] = (
        SCL_VEGETATION, SCL_NOT_VEGETATED, SCL_WATER, SCL_UNCLASSIFIED)

    # --- digital numbers to reflectance ------------------------------------
    # L2A is distributed as integers. Processing baseline 04.00 (2022-01-25
    # onward) also carries BOA_ADD_OFFSET = -1000, and because MNDWI and NDVI
    # are normalised ratios an offset error corrupts them outright (a pure
    # scale factor cancels; an offset does not). These are the FALLBACK values,
    # used only when the file itself declares no scale/offset; what was
    # actually applied is recorded in run_report.json.
    reflectance_scale: float = 10000.0
    boa_add_offset: float = -1000.0
    # Whether the specification offset above still NEEDS applying is decided
    # per file from the pixels, not assumed - see optical.dn_offset. A product
    # that has already been offset-corrected (which is what the Copernicus Data
    # Space openEO backend returns, measured 2026-10-11) would be shifted a
    # second time, by a tenth of the whole reflectance range, and a normalised
    # index cannot survive that. The test is that reflectance is a non-negative
    # physical quantity: L2A over-correction pushes a SMALL share of dark
    # pixels below zero, so an offset applied twice usually announces itself as
    # a large one. Measured on the dev-set files: 0.0-2.0% of valid pixels under
    # the correct convention against 9.1-62.8% under the doubled one - on 19 of
    # the 20.
    #
    # THE TWENTIETH IS THE ONE TO UNDERSTAND. `ecuador_poza_honda_control_2024`
    # 2024-08-31 is 87% cloud and has no dark tail, so the doubled offset costs
    # it only 1.70% - BELOW the 1.96% worst case of a correctly converted file.
    # The two populations therefore OVERLAP, and on that file this test cannot
    # decide at all. What keeps it harmless is not this margin but
    # min_clear_fraction, which refuses the pair at 12.7% clear long before an
    # index is computed; tests/test_optical_real_files.py asserts that
    # disjunction - a file is either decidable or too cloudy to use.
    #
    # So: a sound guard against the error that actually occurred, not a
    # universal discriminator. Set at the same 5% as max_implausible_fraction
    # and from the same argument - it sits ~2.5x above the worst correct
    # observation and ~1.8x below the lowest incorrect one among the 19 the
    # test can separate - and not from any dev-event score
    # (docs/tuning_log.md, 2026-10-12).
    max_negative_fraction: float = 0.05
    # Values above this are digital numbers, not reflectance. Physical
    # reflectance cannot exceed ~1.6 even over snow and specular glint.
    assume_dn_above: float = 1.5
    # Refuse the optical stage when more than this share of valid pixels lands
    # outside plausible reflectance after conversion - a wrong scale or offset
    # must stop the stage, not quietly produce wrong indices.
    plausible_reflectance_range: tuple[float, float] = (-0.2, 1.6)
    max_implausible_fraction: float = 0.05

    # --- indices -----------------------------------------------------------
    # Water: MNDWI above a threshold AND a real rise since the pre-event date.
    # Xu (2006) proposes MNDWI > 0 to separate water from built-up land and
    # soil; 0.0 is therefore the literature value. SEARCHED by
    # OPTICAL_TUNE_GRID, because flood water in a Himalayan gorge is heavily
    # sediment-laden and its MNDWI is lower than that of clear water.
    mndwi_water: float = 0.0
    # The RISE requirement is what makes this a change detector rather than a
    # water classifier: permanent water and terrain shadow have a high or
    # ambiguous MNDWI on BOTH dates and so never qualify. 0.20 is four times
    # the ~0.05 index scatter that atmospheric and BRDF differences between two
    # clear dates produce; not set from any score.
    mndwi_rise_min: float = 0.20

    # Debris, mud and deposits: vegetation or soil buried by sediment loses
    # NDVI and gains SWIR brightness. 0.15 was the value set from phenology -
    # a loss well outside seasonal change over a few weeks - and 0.10/0.20
    # were SEARCHED around it by OPTICAL_TUNE_GRIDS.
    #
    # 0.10 since 2026-10-12, by the Pareto-dominance rule
    # (eval/chosen_params.json, run_id 20261012T165259Z, hash 0550873e7ea6).
    #
    # It was 0.20 earlier the same day, chosen when debris was still part of the
    # flood extent: the stricter value won on all six folds because the deposit
    # channel's false positives counted against the score. With
    # debris_in_flood_extent now False those pixels no longer reach the scored
    # quantity at all, so the threshold no longer trades controls against
    # floods and the looser value is simply better on the floods. The two
    # changes belong together and were adopted together; splitting them would
    # reinstate the trade.
    ndvi_drop_min: float = 0.10
    # Fresh mud and boulder deposits are brighter in SWIR than the vegetation
    # or wet soil they bury. 0.03 reflectance is roughly three times the
    # radiometric uncertainty of an L2A SWIR band. Not searched.
    swir_rise_min: float = 0.03

    def __post_init__(self) -> None:
        if self.fusion not in OPTICAL_FUSION_MODES:
            raise ValueError(
                f"optical fusion {self.fusion!r} is not supported; "
                f"choose one of {', '.join(OPTICAL_FUSION_MODES)}.")
        if not 0.0 <= self.min_clear_fraction <= 1.0:
            raise ValueError(
                f"min_clear_fraction must be in [0, 1], got {self.min_clear_fraction!r}.")
        if not self.scl_valid_classes:
            raise ValueError("scl_valid_classes is empty: no pixel could ever be used.")
        lo, hi = self.plausible_reflectance_range
        if not lo < hi:
            raise ValueError(
                f"plausible_reflectance_range must be (low, high), got {(lo, hi)!r}.")
        if self.max_post_latency_days < 0 or self.search_days_before <= 0:
            raise ValueError("optical search windows must be positive.")


# Per-pixel provenance of a flood-affected pixel. Its own small raster, NOT
# mixed into the class map: a reader must be able to see WHAT a pixel is and
# WHICH SENSOR said so independently (the same separation as the reliability
# layer).
SOURCE_NONE = 0        # not flood-affected
SOURCE_SAR = 1         # Sentinel-1 only
SOURCE_OPTICAL = 2     # Sentinel-2 only
SOURCE_BOTH = 3        # both sensors agree

SOURCE_NAMES = {
    SOURCE_NONE: "not flood-affected",
    SOURCE_SAR: "Sentinel-1 radar only",
    SOURCE_OPTICAL: "Sentinel-2 optical only",
    SOURCE_BOTH: "Sentinel-1 and Sentinel-2 agree",
}


@dataclass(frozen=True)
class ReliabilityParams:
    """Where the map should be read with less confidence. Reporting only.

    This changes NO detection. It is a DEM-only statement about where the
    ground is too narrow for the sensor to resolve, computed and written
    alongside the classes so a dashboard, a copilot answer or a sitrep can
    qualify a number instead of presenting it flat.
    """

    # A valley floor this many pixels wide or narrower cannot be resolved by a
    # 20 m sensor: a single resolution cell then straddles water and wall, so
    # every pixel in it is a mixture and its class is a guess about which
    # scatterer dominated. Set from the imaging geometry, not from any score -
    # at the shipped 20 m grid, 3 px is 60 m, and the measured failure this
    # layer exists to flag (docs/tuning_log.md, 2026-10-11) is in gorges of
    # roughly that width. Width is in PIXELS, so it follows resolution_m.
    min_valley_width_px: float = 3.0

    def __post_init__(self) -> None:
        if not self.min_valley_width_px > 0:
            raise ValueError(
                f"min_valley_width_px must be positive, got {self.min_valley_width_px!r}.")


# --- OpenStreetMap (ohsome) ---------------------------------------------------

# The OSM layers the damage overlay and the cut-off analysis need, as ohsome
# filter expressions. Declared here, once, so the pipeline and the evaluation
# ask for exactly the same features and a reader can see what "a building" or
# "a road" means in this system.
#
# `health` follows the two tagging schemes that coexist in OSM: the older
# `amenity=*` and the `healthcare=*` scheme. `health_post` appears under both,
# which is why both are listed rather than one being assumed canonical.
OSM_LAYERS = {
    "buildings": "building=* and geometry:polygon",
    "roads": "highway=* and geometry:line",
    "bridges": "bridge=* and geometry:line",
    "waterways": "waterway=* and geometry:line",
    "places": ("place in (city, town, village, hamlet, suburb, neighbourhood, "
               "isolated_dwelling, farm) and (geometry:point or geometry:polygon)"),
    "health": ("(amenity in (hospital, clinic, doctors, health_post) or "
               "healthcare in (hospital, clinic, doctor, health_post, centre)) and "
               "(geometry:point or geometry:polygon)"),
}


@dataclass(frozen=True)
class OsmParams:
    """Which OSM snapshot to read, over what area, from where.

    NOT in params_hash, for the ReliabilityParams reason: no field here can
    change a pixel of the flood class map. The snapshot date DOES change the
    damage overlay, so it is recorded in run_report.json under `osm`, which is
    where a reader must be able to see which OSM the answer was built from.
    """

    # Rule 1: the OSM snapshot must predate the event, so no post-event mapper
    # response can leak into a pipeline input. The ohsome instance's own
    # temporal extent ends at 2026-07-27T09:00Z (its /metadata endpoint, read
    # 2026-10-13), which is the same date CLAUDE.md records as the latest
    # available snapshot, so a date after this cannot be served anyway.
    latest_snapshot: str = "2026-07-27"

    # Context beyond the mapped area, because a settlement is cut off by a road
    # or bridge that may lie outside the flood bbox, and a route out of the area
    # leaves it by definition. 15 km is a declared operational choice - roughly
    # a half-day walk out of a valley - not a tuned value, and it is applied
    # identically to every area.
    buffer_km: float = 15.0

    base_url: str = "https://api.ohsome.org/v1"
    timeout_s: float = 300.0
    max_retries: int = 2
    retry_backoff_s: float = 5.0

    # ohsome's WAF answers 403 to an unidentified client, so a User-Agent is
    # required rather than polite. It names the project and that this is an
    # educational prototype.
    user_agent: str = ("strandline/0.1 (IIT Mandi Multimodal AI Hackathon 2026, "
                       "educational prototype; OpenStreetMap via ohsome)")

    def __post_init__(self) -> None:
        if self.buffer_km < 0:
            raise ValueError(f"buffer_km must not be negative, got {self.buffer_km!r}.")
        if self.timeout_s <= 0:
            raise ValueError(f"timeout_s must be positive, got {self.timeout_s!r}.")
        # Fails loudly here rather than at the first request.
        date.fromisoformat(self.latest_snapshot)


# --- Damage overlay -----------------------------------------------------------

# The wording every "likely out" bridge carries, in every output. A bridge is
# never confirmed destroyed from radar and a DEM: what is established is that
# the channel it crosses was flooded, which is a reason to check it, not a
# finding that it is gone.
BRIDGE_UNVERIFIED_LABEL = "likely out, unverified"


@dataclass(frozen=True)
class DamageParams:
    """How the flood extent is intersected with OSM features.

    NOT in params_hash, for the same reason as ReliabilityParams: nothing here
    changes a pixel of the flood class map. Every value is recorded in
    run_report.json under `damage.parameters`.
    """

    # How far the flood extent is grown before asking whether a feature touches
    # it, in PIXELS of the analysis grid (1 px = 20 m at the shipped 20 m
    # resolution, so it follows resolution_m rather than fixing a metre value).
    #
    # Set from GEOLOCATION ACCURACY and never from a score. Two errors stack
    # between a flood pixel and an OSM footprint: Sentinel-1 IW GRDH
    # orthorectification residual over terrain, and the positional accuracy of
    # OSM geometry itself, which for buildings traced from consumer imagery is
    # commonly several metres. One 20 m cell is the smallest tolerance the
    # analysis grid can express and is the same 1 px the gates file declares as
    # its boundary tolerance, so the overlay is as forgiving as the scoring is
    # and no more. It is NOT a sensitivity dial: raising it would flag more
    # buildings and must not be done to move a number.
    buffer_px: int = 1

    # A bridge is judged on the channel it crosses, within this distance of the
    # crossing point, rather than on the whole waterway: a river can be in flood
    # a kilometre downstream and say nothing about this crossing. 100 m is about
    # five analysis cells, enough to cover a crossing and its immediate
    # approaches at 20 m. Declared, not tuned.
    bridge_reach_m: float = 100.0

    # Road fragments shorter than this are dropped after junction splitting.
    # Node-splitting a network produces slivers where ways meet at slightly
    # different coordinates; counting those as "segments" would inflate both the
    # cut count and the total. 5 m is well below any real road segment and well
    # above a coordinate-rounding sliver.
    min_segment_m: float = 5.0

    def __post_init__(self) -> None:
        if self.buffer_px < 0:
            raise ValueError(f"buffer_px must not be negative, got {self.buffer_px!r}.")
        if self.bridge_reach_m <= 0:
            raise ValueError(f"bridge_reach_m must be positive, got {self.bridge_reach_m!r}.")
        if self.min_segment_m < 0:
            raise ValueError(f"min_segment_m must not be negative, got {self.min_segment_m!r}.")


def params_dict(detect: DetectParams, run: RunParams,
                optical: "OpticalParams | None" = None,
                extent: "ExtentParams | None" = None) -> dict:
    """The parameters that PRODUCED the classes, i.e. what params_hash covers.

    The `optical` section appears ONLY when optical evidence was enabled, and
    NO optical field lives in DetectParams or RunParams. That is not cosmetic:
    it is what keeps every params_hash already recorded in eval/results/
    reproducible, because a radar-only configuration hashes byte-identically to
    the way it did before Sentinel-2 existed in this pipeline. Pinned by
    tests/test_params_hash_reproducible.py against the hashes actually published
    in eval/chosen_params.json, eval/results/flood.csv and docs/tuning_log.md.

    A run that consulted Sentinel-2 is a different configuration and gets a
    different hash, whether or not a clear scene turned out to be available -
    what the run DID with the data it found is recorded separately, in
    run_report.json's `optical.status`.

    ReliabilityParams is deliberately NOT in here. It cannot change a single
    output pixel of the class map, so folding it into params_hash would give a
    new hash to an unchanged detection and make every already-recorded row in
    eval/results/ look like a different configuration. It is written to
    run_report.json under its own `reliability.parameters` key instead, so it
    is still fully recorded.

    The `extent` section is the one case that needs a rule of its own, and it is
    the reverse of ReliabilityParams. ExtentParams changes no pixel of the class
    map either - debris is written as CLASS_DEBRIS whatever it says - but it DOES
    change the scored quantity, so two configurations that score differently must
    not share a hash. It is therefore included, but ONLY WHEN IT DIFFERS FROM
    `EXTENT_HASH_BASELINE`: baseline in, nothing added, every hash published
    before the option existed stays reproducible; any other value, its own
    section and its own hash.

    The baseline is a FIXED CONSTANT and emphatically not `ExtentParams()`, the
    live default. An earlier version compared against the live default, which
    works until the day the default moves - and then a configuration that scores
    a DIFFERENT quantity silently hashes like the rows already published, which
    is the exact failure this whole arrangement exists to prevent. Keying off a
    constant makes the published hashes depend on what was measured rather than
    on what happens to be shipped today. Pinned by
    tests/test_params_hash_reproducible.py.
    """
    out = {"detect": asdict(detect), "run": asdict(run)}
    if optical is not None and optical.enabled:
        out["optical"] = asdict(optical)
    if extent is not None and extent != EXTENT_HASH_BASELINE:
        out["extent"] = asdict(extent)
    return out


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

# The same thing with the debris class taken out, for
# ExtentParams.debris_in_flood_extent = False. Debris is still written to
# flood_classes.tif either way: what this changes is whether it counts as
# flood EXTENT.
FLOOD_EXTENT_WITHOUT_DEBRIS = (CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM)

# Wording required wherever a debris number is shown while it is NOT part of
# the flood extent. One source string, so the dashboard, the copilot and the
# sitrep cannot describe it three different ways (as NARROW_VALLEY_CAVEAT does
# for the reliability layer).
DEBRIS_UNVERIFIED_LABEL = "possible debris, unverified"


@dataclass(frozen=True)
class ExtentParams:
    """What counts as FLOOD EXTENT, as opposed to what is merely mapped.

    Its own dataclass, and not a field of DetectParams, for the reason recorded
    in docs/tuning_log.md on 2026-10-11: a field on DetectParams re-hashes every
    configuration ever published, and this flag changes no pixel of the class
    map. But it DOES change the scored quantity, so it must be able to change
    params_hash - which is why `params_dict` includes this section only when it
    differs from the default. Default in, default out: every hash already
    published stays reproducible.
    """

    # FALSE since 2026-10-12, adopted by the Pareto-dominance rule
    # (eval/chosen_params.json, run_id 20261012T165259Z, hash 0550873e7ea6):
    # in every one of the six leave-one-event-out folds, debris-OUT is at least
    # as good as debris-IN on mean training tolerant F1 AND on both false-flood
    # fractions of all four controls, and strictly better on 24 of those
    # quantities. Nothing was traded.
    #
    # So: debris stays an OUTPUT LAYER, written to flood_classes.tif and
    # reported under DEBRIS_UNVERIFIED_LABEL, but it is NOT flood extent - not
    # scored, not in flood-affected hectares, and NOT to be consumed by the
    # damage overlay or the cut-off analysis.
    #
    # Why, in one line: the debris channel's measured precision against the
    # only expert mass-movement reference in the dev set is 0.0 (18 predicted,
    # 0 correct, 582 missed), and 96% of all its dev-set pixels fell on a dry
    # no-flood control.
    #
    # True remains available and remains the HASH BASELINE (see
    # EXTENT_HASH_BASELINE): every params_hash published before this option
    # existed was measured with debris inside the extent.
    #
    # The REFERENCE definition does not follow this flag: references are still
    # scored as flooded + debris/mass-movement, so False makes the task HARDER
    # (reference debris becomes an unreachable miss) and won anyway.
    debris_in_flood_extent: bool = False

    def flood_extent_classes(self) -> tuple[int, ...]:
        return (FLOOD_AFFECTED_CLASSES if self.debris_in_flood_extent
                else FLOOD_EXTENT_WITHOUT_DEBRIS)


# The extent configuration that EVERY params_hash published before this option
# existed was measured at: debris inside the flood extent, as decided on
# 2026-10-11. `params_dict` omits the `extent` section exactly when the
# configuration equals this, so those hashes stay reproducible for good.
#
# It is deliberately a constant and NOT `ExtentParams()`. The shipped default
# moved to debris-OUT on 2026-10-12 and may move again; the baseline must not
# follow it, or a configuration scoring a different quantity would hash like the
# rows already published. Changing this constant invalidates every published
# hash and must never be done to make a diff smaller.
EXTENT_HASH_BASELINE = ExtentParams(debris_in_flood_extent=True)


# --- Dev-event scoring cost ---------------------------------------------------
#
# Scoring the dev set means downloading a Sentinel-1 stack per event per
# backscatter coefficient, so stack depth is a download-cost setting as well as
# a detection one. 3 is `RunParams.min_pre`, the thinnest stack the pipeline
# will accept, and one dev event (the steepest) has only 3 pre images available
# anyway, so the dev set could not have been scored deeper without dropping it.
#
# Since 2026-10-13 this is an ALIAS of `N_PRE`, not an independent value: the
# shipped default and the scored configuration are the same stack depth, so a
# default run reproduces the evaluated params_hash. The full rationale, and the
# unmeasured cost of having chosen 3 over 5, is at `N_PRE` above. Nothing here
# may give it a value of its own; tests/test_shipped_defaults.py enforces that.
#
# Every params_hash still records n_pre, so any row can be traced back to the
# stack depth used.
DEV_N_PRE = N_PRE


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


# --- Optical parameter search grid (eval/tune.py, leave-one-event-out) -------
#
# DECLARED BEFORE A SINGLE OPTICAL SCORE EXISTS (docs/tuning_log.md, 2026-10-11)
# and deliberately tiny: 5 points in total.
#
# ONE GRID PER FUSION MODE, exactly as TUNE_GRIDS is one grid per detection
# mode, so that no point ever varies a parameter its mode ignores: with fusion
# `off` an MNDWI threshold cannot change a single pixel, so `off` is ONE point
# rather than four identical ones. The keys ARE the fusion modes, so
# `optical.fusion` must not also appear on an axis.
#
# `off` is in the grid because the honest comparison has to be possible: does
# optical evidence help at all? It must be able to win.
#
# This grid is searched as a SECOND leave-one-event-out pass, crossed with the
# SAR point already chosen for that fold, rather than folded into TUNE_GRIDS.
# That is a cost decision - a joint search would be 12 x 5 = 60 detections per
# event per fold instead of 5 - taken in advance and written down, not after
# seeing a score. Each fold's SAR point is still the one chosen without the
# held-out event, so the result stays out of sample.
#
#   optical.mndwi_water     0.0 is Xu (2006). 0.2 asks whether a stricter
#                           threshold is needed once the rise test already
#                           removes permanent water.
#   optical.ndvi_drop_min   0.10 / 0.20 around the 0.15 set from phenology.
#
# Everything else - the SCL class list, the MNDWI rise, the SWIR rise, the
# scene-selection windows - stays at its specification- or literature-set value.
# Adding an axis must be justified here, not by a score.
OPTICAL_TUNE_GRIDS: dict[str, dict[str, tuple]] = {
    FUSION_OFF: {},                                      # 1 point
    FUSION_UNION: {                                      # 2 x 2 = 4
        "optical.mndwi_water": (0.0, 0.2),
        "optical.ndvi_drop_min": (0.10, 0.20),
    },
    # Added 2026-10-12, DECLARED BEFORE SCORING (docs/tuning_log.md): the water
    # half of the optical evidence without the deposit channel. `ndvi_drop_min`
    # is deliberately absent - with no deposit channel it cannot change a pixel,
    # so varying it would score the same map twice.
    FUSION_WATER_ONLY: {                                 # 2 points
        "optical.mndwi_water": (0.0, 0.2),
    },
}


# --- What counts as flood extent (eval/tune.py, second pass) -----------------
#
# DECLARED BEFORE SCORING on 2026-10-12 (docs/tuning_log.md). Crossed with every
# fusion point above, so the second pass is 7 x 2 = 14 points.
#
# On the grid rather than simply switched off because the class-count audit
# measured the debris channel at precision 0.0 against the only expert
# mass-movement reference in the dev set, with 96% of its dev-set pixels on a
# dry control - strong evidence, but this project scores a decision when it can
# instead of taking it on judgement.
#
# `False` is the HARDER option by construction: the reference side does not
# follow the flag, so reference debris becomes an unreachable miss. It is
# allowed to lose on that basis.
EXTENT_TUNE_GRID: dict[str, tuple] = {
    "extent.debris_in_flood_extent": (True, False),
}


# --- The control feasibility criterion (eval/tune.py) ------------------------
#
# DECLARED BEFORE SCORING on 2026-10-12. A parameter set is feasible only if
# EVERY no-flood control passes BOTH:
#
#   false_flood_fraction                 whole scoring domain, from the
#                                        human-owned gates file
#   false_flood_fraction_analysed_area   the domain MINUS terrain-excluded
#                                        ground, declared here
#
# Why the second exists: the class-count audit measured
# bhutan_punakha_control_2024 at 0.0022 over the whole domain and 4.37% over the
# ground the detector could actually classify, because 95% of that AOI is
# terrain-excluded and pads the denominator. A 1% ceiling that a 4.37%
# false-flood rate passes is not doing its job.
#
# The gates file is NOT touched and the gated metric, the reference definition,
# the scoring domain and the boundary tolerance are all unchanged. This is an
# ADDITIONAL constraint inside the tuner and can only make adoption harder -
# which is the opposite of what Rule 3.2 forbids.
#
# The value is not duplicated from the gates file: the whole-domain ceiling is
# READ from it (eval/tune.py::control_ceiling), and this is the analysed-area
# twin, deliberately set to the same 1% so the two cannot drift apart in meaning.
CONTROL_ANALYSED_AREA_CEILING = 0.01


# --- When optical fusion may become the default ------------------------------
#
# DECLARED BEFORE THE OPTICAL PASS WAS RUN (docs/tuning_log.md, 2026-10-12), so
# the bar cannot be moved to fit the result. Winning the mean is NOT enough:
# fusion only ever ADDS pixels, so it can buy recall on one event while
# quietly costing precision on another and raising the false-flood rate on a
# dry valley, and the mean would hide both.
#
#   min_mean_gain          the mean held-out boundary-tolerant F1 must actually
#                          improve. 0.0 means "strictly greater": a tie is not
#                          an improvement, and the simpler configuration wins a
#                          tie because it is the one the dev numbers measured.
#   max_event_regression   no single flood event may lose more than this much
#                          tolerant F1, and no control may gain more than this
#                          much false-flood fraction. 0.01 is the project's own
#                          false-flood ceiling, reused deliberately: a change
#                          that costs any one event or control as much as the
#                          entire error budget allows is not an improvement
#                          however well it averages.
#
# The absolute control ceiling (every control <= 1% false flood) is NOT
# repeated here - it is read from eval/gates.yaml, which is human-owned, so the
# two can never drift apart.
OPTICAL_ADOPTION = {
    "min_mean_gain": 0.0,
    "max_event_regression": 0.01,
}


# --- Reliability layer class codes (uint8 GeoTIFF) ---------------------------
#
# Its own small raster, NOT mixed into the class map: a reader must be able to
# see the class and the confidence qualifier independently.
RELIABILITY_OUTSIDE_VALLEY = 0   # not valley floor by HAND and slope
RELIABILITY_RESOLVED = 1         # valley floor, wider than min_valley_width_px
RELIABILITY_NARROW_VALLEY = 2    # valley floor, narrower: low confidence

RELIABILITY_NAMES = {
    RELIABILITY_OUTSIDE_VALLEY: "not valley floor",
    RELIABILITY_RESOLVED: "valley floor, resolved",
    RELIABILITY_NARROW_VALLEY: "low confidence: valley narrower than radar resolution",
}

# The exact wording the dashboard, the copilot and the sitrep must use for a
# number that falls inside the narrow-valley zone. Kept here so the three of
# them cannot drift apart, and so the Nepali translation has one source string.
NARROW_VALLEY_CAVEAT = RELIABILITY_NAMES[RELIABILITY_NARROW_VALLEY]
