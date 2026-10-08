"""Terrain layers from the Copernicus DEM: slope, drainage network, HAND.

HAND (height above nearest drainage) = how far above the closest channel a
pixel sits, following the D8 flow path. Flood water cannot sit 200 m up a
valley wall, so HAND removes most mountain false positives (radar shadow,
wet snow, dark rock) that fool simple thresholding.
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from scipy import ndimage
from rasterio.crs import CRS
from rasterio.transform import Affine


@dataclass
class Terrain:
    slope_deg: np.ndarray
    hand_m: np.ndarray
    streams: np.ndarray        # bool, channel pixels
    flow_dir: np.ndarray       # D8 codes (pysheds dirmap), for flood-path tracing
    accumulation: np.ndarray   # upstream cell count
    hand_fallback_fraction: float = 0.0


def slope_degrees(dem: np.ndarray, pixel_size_m: float) -> np.ndarray:
    filled = np.where(np.isfinite(dem), dem, np.nanmean(dem))
    dz_dy, dz_dx = np.gradient(filled, pixel_size_m)
    slope = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))
    slope[~np.isfinite(dem)] = np.nan
    return slope


def _numpy_compat_for_pysheds() -> None:
    """pysheds 0.5 still calls np.in1d, removed in NumPy 2.4."""
    if not hasattr(np, "in1d"):
        np.in1d = lambda a, b, assume_unique=False, invert=False: np.isin(  # type: ignore[attr-defined]
            np.ravel(a), b, assume_unique=assume_unique, invert=invert)


def compute_terrain(dem: np.ndarray, transform: Affine, crs: CRS,
                    stream_threshold_km2: float) -> Terrain:
    _numpy_compat_for_pysheds()
    from pysheds.grid import Grid  # imported here: heavy (numba)

    pixel_size = abs(transform.a)
    nodata = -9999.0
    dem_f = np.where(np.isfinite(dem), dem, nodata).astype(np.float64)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dem.tif"
        with rasterio.open(path, "w", driver="GTiff", height=dem.shape[0], width=dem.shape[1],
                           count=1, dtype="float64", crs=crs, transform=transform,
                           nodata=nodata) as dst:
            dst.write(dem_f, 1)
        grid = Grid.from_raster(str(path))
        raster = grid.read_raster(str(path))

    pit_filled = grid.fill_pits(raster)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)
    fdir = grid.flowdir(inflated)
    acc = grid.accumulation(fdir)

    threshold_cells = stream_threshold_km2 * 1e6 / (pixel_size * pixel_size)
    streams_raster = acc > threshold_cells          # stays a pysheds Raster
    hand = np.asarray(grid.compute_hand(fdir, raster, streams_raster), dtype=np.float64)
    streams = np.asarray(streams_raster, dtype=bool).copy()

    invalid = ~np.isfinite(dem)
    streams &= ~invalid

    # Cells whose flow path leaves the area before reaching a channel get no
    # HAND from pysheds. Fallback: height above the nearest channel pixel
    # (straight-line). Reported, so its use is never hidden.
    missing = ~np.isfinite(hand) & ~invalid
    fallback_fraction = 0.0
    if missing.any() and streams.any():
        _, (ri, ci) = ndimage.distance_transform_edt(~streams, return_indices=True)
        nearest_height = np.where(invalid, np.nan, dem)[ri, ci]
        hand[missing] = dem[missing] - nearest_height[missing]
        fallback_fraction = float(missing.sum() / max((~invalid).sum(), 1))

    hand[invalid] = np.nan
    hand[np.isfinite(hand) & (hand < 0)] = 0.0
    return Terrain(slope_deg=slope_degrees(dem, pixel_size), hand_m=hand, streams=streams,
                   flow_dir=np.asarray(fdir), accumulation=np.asarray(acc),
                   hand_fallback_fraction=round(fallback_fraction, 4))
