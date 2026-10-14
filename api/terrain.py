"""Terrarium-encoded terrain tiles from a run's own cached Copernicus DEM.

The dashboard renders the valley in 3D. MapLibre's `raster-dem` source reads
elevation out of the RGB channels of a PNG, and the encoding it understands
without a plugin is Mapzen's *terrarium*:

    v = elevation_m + 32768
    R = v // 256
    G = v % 256
    B = round(fraction * 256)

so 0 m is (128, 0, 0) and one metre is one G step. We serve those tiles from
`runs/<run_id>/cache/dem_20m.tif` — the DEM the pipeline already downloaded and
resampled for this exact area (Rule 1: Copernicus DEM, no new source, no
network at request time). A tile that falls outside the DEM comes back flat at
0 m rather than as an error, because a hole in a terrain source makes MapLibre
tear the mesh.
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

TILE_SIZE = 256

#: The DEM file inside a run directory, in the order worth trying.
DEM_CANDIDATES = ("cache/dem_20m.tif", "cache/dem.tif", "dem_20m.tif")

#: Shown next to the map whenever these tiles are on screen.
TERRAIN_ATTRIBUTION = "Copernicus DEM"


def find_dem(run_dir: Path) -> Path | None:
    """The run's own cached DEM, or None if this run never fetched one."""
    for name in DEM_CANDIDATES:
        candidate = Path(run_dir) / name
        if candidate.exists():
            return candidate
    return None


def encode_terrarium(elevation_m: np.ndarray) -> np.ndarray:
    """Elevation in metres -> a (h, w, 3) uint8 terrarium array.

    Anything not finite (the DEM's nodata is NaN) is encoded as 0 m, which is
    flat rather than a spike.
    """
    elevation = np.asarray(elevation_m, dtype="float64")
    elevation = np.where(np.isfinite(elevation), elevation, 0.0)
    # Terrarium covers -32768..32767 m; clip so a corrupt pixel cannot wrap
    # around into a mountain.
    shifted = np.clip(elevation + 32768.0, 0.0, 65535.999)
    whole = np.floor(shifted)
    red = np.floor(whole / 256.0)
    green = whole - red * 256.0
    blue = np.floor((shifted - whole) * 256.0)
    return np.stack([red, green, blue], axis=-1).astype("uint8")


def _to_png(rgb: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def flat_tile_png(elevation_m: float = 0.0, tilesize: int = TILE_SIZE) -> bytes:
    """A tile that is one elevation everywhere: what we serve outside the DEM.

    The default is sea level; callers pass the DEM's own base elevation so the
    surround meets the mapped area flush instead of as a cliff.
    """
    rgb = encode_terrarium(np.full((tilesize, tilesize), float(elevation_m), dtype="float64"))
    return _to_png(rgb)


@lru_cache(maxsize=32)
def _base_elevation(dem_key: tuple[str, int, int]) -> float:
    """The DEM's lowest elevation, read once from a decimated overview.

    This is the height the ground outside the mapped area is drawn at. Picking
    the minimum means the surround never rises above the valley floor and never
    hides anything; it is the map's edge, not a claim about the terrain there.
    """
    path = dem_key[0]
    try:
        with rasterio.open(path) as src:
            sample = src.read(1, out_shape=(1, 256, 256), masked=True).astype("float64")
        finite = np.asarray(sample.filled(np.nan))
        finite = finite[np.isfinite(finite)]
        return float(np.min(finite)) if finite.size else 0.0
    except Exception:
        return 0.0


def base_elevation(dem_path: Path) -> float:
    stat = Path(dem_path).stat()
    return _base_elevation((str(dem_path), stat.st_mtime_ns, stat.st_size))


def terrarium_tile_png(dem_path: Path, z: int, x: int, y: int,
                       tilesize: int = TILE_SIZE) -> bytes:
    """One terrarium PNG for the web-mercator tile (z, x, y).

    Outside the DEM's footprint this returns a flat tile at the DEM's base
    elevation instead of raising, so the terrain mesh neither tears nor steps
    off a cliff at the edge of the mapped area.
    """
    floor = base_elevation(dem_path)
    try:
        with Reader(str(dem_path)) as src:
            image = src.tile(x, y, z, tilesize=tilesize, resampling_method="bilinear")
    except TileOutsideBounds:
        return flat_tile_png(floor, tilesize)

    elevation = image.data[0].astype("float64")
    if image.mask is not None:
        # rio-tiler masks are 255 where valid; the rest is outside the DEM and
        # is drawn at the same base elevation as the surround.
        elevation = np.where(np.asarray(image.mask) > 0, elevation, floor)
    elevation = np.where(np.isfinite(elevation), elevation, floor)
    return _to_png(encode_terrarium(elevation))
